"""DocTable: one of the app's tables, kept as Nostr documents on THIS node's relay (#161: the app tables leave
Postgres for Nostr documents, owner's call 2026-10-10).

A row is one kind-30078 document `pcai:t:<table>:<key>`, signed by and NIP-44-encrypted to the node's OPERATOR key
(the keyfile -- never a database row), so only this node can read it and the relay's prune never touches it
(30078 is not prunable, the operator is preserved, nothing here carries an `expiration`).

Every process holds the whole table in memory, because callers filter and sort it the way they used to with SQL:
  * it is LOADED strictly (`nostr_store.list_all_docs`: every document or an exception -- never a short answer);
  * it stays CURRENT through a live subscription to the operator's documents, so a row written by another
    process (the worker, the bot manager) appears here within moments, and a NIP-09 deletion removes it;
  * a WRITE goes to the relay first and touches memory only once the relay accepted it.

What it never does is answer "no rows" for "could not ask": until the first strict load has succeeded, every read
raises `Unavailable`, and a write that the relay refused raises too. The callers decide what unavailable means to
them (refuse, retry, show "unknown") -- the same rule relay_reader keeps for single reads.

THE MOVE FROM SQL, WITHOUT AN OUTAGE (wave 1). A table that came from Postgres has a `Legacy` bound to it
(app/services/table_migration.py binds them at startup). Until that table's `_migrated` marker exists:
  * SQL is AUTHORITATIVE: every read is answered from SQL (through the Legacy), exactly as before the move;
  * every write goes to SQL FIRST and then to the relay, so the relay's copy converges and nothing written while
    the one-time copy runs is lost. A relay half that fails is not an error then (SQL is the store of record and
    the copy will carry it across) -- unless the marker appeared meanwhile, which is re-checked strictly.
Once the marker is written the relay is authoritative and SQL is never read or written again.

AFTER A RESTART a big table takes a while to load (measured ~1 ms a row). The load runs on a THREAD of its own
(`start_background_load`), never on the request loop, and while it runs:
  * a POINT read (`get`/`aget`: an API key's index entry, one blob, a reminder by id) reads that ONE document from
    the relay, strictly -- None only when the relay answered and holds nothing;
  * a WHOLE-TABLE read (`all`, `where`, `view` ...) raises `Loading` (an Unavailable carrying `retry_after`),
    which the routes answer 503 + Retry-After. Never an answer from part of a table.

Rows are plain JSON-able dicts. Keys are strings the caller chooses (an id, an npub, a sha256) and must not contain
anything a d-tag cannot hold; `key()` below makes any value safe.
"""
import asyncio
import json
import logging
import sys
import threading
import time
import urllib.parse

from app.services import nostr_store
from app.services.relay_reader import Unavailable, relay_port

logger = logging.getLogger(__name__)

NS = "pcai:t:"
KIND = nostr_store.APP_KIND
_RESYNC_S = 600            # a full strict reload as a backstop for anything the live stream missed


def key(value) -> str:
    """Any value as a d-tag-safe key (percent-encoded; ':' kept so composite keys stay readable)."""
    return urllib.parse.quote(str(value), safe=":@.-_")


def _compact(value):
    """The same row, with its dict keys and short strings INTERNED. A whole table is held in memory, and JSON
    decoding gives every row its own copy of every key ("pubkey", "size" ... ) and of every repeated value (an
    owner's 64-hex pubkey, a MIME type): measured on 100k blob rows that was about a third of the table's
    footprint. Equal strings compare and hash identically, so nothing reading a row can tell."""
    if isinstance(value, dict):
        return {sys.intern(k) if isinstance(k, str) else k: _compact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    if isinstance(value, str) and len(value) <= 64:
        return sys.intern(value)
    return value


def _operator():
    from app.services import settings_store
    from app.services.nostr import bip340
    sk = settings_store._operator_seckey(None)
    if not sk:
        raise Unavailable("this node has no operator key")
    return sk, bip340.pubkey_from_seckey(sk).hex()


def _in_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _run(coro):
    """Drive a coroutine from synchronous code. Refused on an event-loop thread: blocking it on its own relay
    socket would stall every request this single-worker app serves -- callers there use the async methods."""
    if _in_loop():
        coro.close()
        raise RuntimeError("DocTable sync call on an event-loop thread -- use the async method")
    return asyncio.run(coro)


class Loading(Unavailable):
    """The table is being loaded in the background: ask again in `retry_after` seconds. Whole-table reads only --
    a point read is answered from the relay meanwhile."""

    def __init__(self, msg: str, retry_after: int = 10):
        super().__init__(msg)
        self.retry_after = retry_after


def _norm(row: dict) -> dict:
    """Exactly what a document holds for `row` (its JSON round trip)."""
    return json.loads(json.dumps(row, default=str))


# ====================================================================== the SQL side, until the marker exists
class Legacy:
    """How one table's rows are read and written in SQL until its `_migrated` marker exists.

    `rows(db)` is the whole table as {key: row} -- the SAME rows the migration copies, so a read answered from SQL
    before the marker and one answered from the relay after it are indistinguishable. The other methods default to
    that one where a table has nothing faster to offer."""
    name = ""
    copy = True             # False: a transient table that is marked without a copy and never written to SQL
    int_ids = False         # keys are SQL integer ids: a new row takes SQL's id until the marker exists

    def rows(self, db) -> dict:
        raise NotImplementedError

    def get(self, db, k):
        return self.rows(db).get(str(k))

    def put(self, db, k, row: dict) -> None:
        raise NotImplementedError

    def delete(self, db, k) -> None:
        raise NotImplementedError

    def insert(self, db, row: dict):
        """Insert a NEW row letting SQL assign its id; returns the key. Only for `int_ids` tables."""
        raise NotImplementedError


LEGACIES: dict = {}             # DocTable name -> Legacy (table_migration.register fills it at store import)
# Tables whose store module gates SQL-or-relay ITSELF (app/services/table_gate.py, wave 2: bots, user_settings,
# conversations): no Legacy is bound to the DocTable -- the store writes the relay first and SQL after -- but its
# background loader still waits for the table's `_migrated` marker, like a bound table's (table_migration fills it).
GATED: set = set()
_session_factory = None


def set_session_factory(factory) -> None:
    """The SQL sessions a Legacy is handed. Every process that serves these tables sets it at start
    (table_migration.bind(SessionLocal)); until then a table that still lives in SQL is "could not ask" --
    never read from the relay's incomplete copy, and never from whatever database a default URL points at."""
    global _session_factory
    _session_factory = factory


def _sql(fn, *, write: bool = False):
    """Run `fn(db)` in a session of its own. Any SQL failure is "could not ask" (Unavailable), never "no rows"."""
    factory = _session_factory
    if factory is None:
        raise Unavailable("this process has no SQL session for the tables still being moved off it")
    try:
        db = factory()
    except Exception as e:      # noqa: BLE001
        raise Unavailable("could not open the SQL database: %s" % e) from e
    try:
        out = fn(db)
        if write:
            db.commit()
        return out
    except Unavailable:
        db.rollback()
        raise
    except Exception as e:      # noqa: BLE001
        try:
            db.rollback()
        except Exception:       # noqa: BLE001
            pass
        raise Unavailable("SQL could not be asked: %s: %s" % (type(e).__name__, e)) from e
    finally:
        db.close()


# ====================================================================== migration markers
MARKERS = "_migrated"
_marked: set = set()            # tables this process has seen marked -- monotonic, never asked again
_neg: dict = {}                 # table -> monotonic time of the last "not marked yet" answer from the relay
_NEG_TTL = 2.0


def is_marker(m) -> bool:
    return isinstance(m, dict) and (m.get("ok") is True or m.get("verified") is True)


def _saw_marked(name: str) -> None:
    if name in _marked:
        return
    _marked.add(name)
    _neg.pop(name, None)
    t = DocTable._registry.get(name)
    if t is not None:
        t._became_authoritative()


async def amarked(name: str, *, fresh: bool = False) -> bool:
    """Has `name` been copied from SQL and verified? Raises Unavailable when it cannot be asked -- "could not ask"
    is neither "migrated" nor "not migrated". `fresh` always asks the relay (a write deciding who is authoritative)."""
    if name in _marked:
        return True
    m_table = DocTable._registry.get(MARKERS)
    if not fresh:
        if m_table is not None and m_table.loaded:          # kept current by its live stream
            with m_table._lock:
                m = m_table._rows.get(name)
            if is_marker(m):
                _saw_marked(name)
                return True
            return False
        if time.monotonic() - _neg.get(name, -1e9) < _NEG_TTL:
            return False
    m = await DocTable(MARKERS).aget_remote(name)
    if is_marker(m):
        _saw_marked(name)
        return True
    _neg[name] = time.monotonic()
    return False


def marked(name: str) -> bool:
    """`amarked` for synchronous code. On an event-loop thread it can only answer from memory: unknown there is
    Unavailable, never a guess."""
    if name in _marked:
        return True
    if not _in_loop():
        return _run(amarked(name))
    m_table = DocTable._registry.get(MARKERS)
    if m_table is not None and m_table.loaded:
        with m_table._lock:
            m = m_table._rows.get(name)
        if is_marker(m):
            _saw_marked(name)
            return True
        return False
    if time.monotonic() - _neg.get(name, -1e9) < _NEG_TTL:
        return False
    raise Unavailable("the migration state of %s is not known in this process yet" % name)


def reset_state() -> None:
    """Forget every marker seen and the SQL session factory (tests: a fresh process)."""
    _marked.clear()
    _neg.clear()
    set_session_factory(None)


class DocTable:
    _registry: dict = {}
    _registry_lock = threading.Lock()

    def __new__(cls, name: str):
        with cls._registry_lock:
            t = cls._registry.get(name)
            if t is None:
                t = super().__new__(cls)
                t._init(name)
                cls._registry[name] = t
            return t

    def _init(self, name: str):
        self.name = name
        self.prefix = "%s%s:" % (NS, name)
        self._rows: dict = {}
        self._stamp: dict = {}          # key -> created_at of the version held (newest wins)
        self._loaded_at = 0.0
        self._lock = threading.RLock()
        self._load_lock = threading.Lock()
        self._live = None
        self._watchers: list = []
        self.legacy = LEGACIES.get(name)    # SQL's side until the table's marker exists (None: relay only)
        self._bg = None                 # the background loader thread, when this process runs one
        self._bg_lock = threading.Lock()
        self._reload_wanted = False
        self.load_seconds = None        # how long the last strict load took (measured, reported)
        self.resync_s = _RESYNC_S       # the backstop full reload (a big table sets a longer one)

    @property
    def loaded(self) -> bool:
        """True once a strict load has succeeded (reads are then answered from memory)."""
        return bool(self._loaded_at)

    @property
    def loading(self) -> bool:
        """A background load is running and has not finished its first pass."""
        return not self._loaded_at and self._bg is not None and self._bg.is_alive()

    def bind_legacy(self, legacy) -> None:
        self.legacy = legacy

    def watch(self, fn) -> None:
        """Call `fn(key, row_or_None)` after every change this process sees (its own writes, the live stream),
        and `fn(None, None)` after a full (re)load. For callers that keep a secondary index (rows by owner);
        `fn` runs on whichever thread made the change and must be quick and must not call back into writes."""
        self._watchers.append(fn)

    def _notify(self, k, row) -> None:
        for fn in list(self._watchers):
            try:
                fn(k, row)
            except Exception as e:      # noqa: BLE001 -- an index bug must not fail the write that fed it
                logger.warning("[doctable] %s watcher failed: %s", self.name, e)

    # ------------------------------------------------------------------ who is authoritative
    async def amigrated(self) -> bool:
        """True when the relay is this table's store of record (no Legacy, or its marker exists)."""
        return self.legacy is None or await amarked(self.name)

    def migrated(self) -> bool:
        return self.legacy is None or marked(self.name)

    def _became_authoritative(self) -> None:
        # Whatever this process held from before the marker was never the store of record: load afresh.
        with self._lock:
            self._loaded_at = 0.0

    # ------------------------------------------------------------------ loading
    async def aload(self, *, force: bool = False) -> None:
        if self._loaded_at and not force:
            return
        sk, pk = _operator()
        # The live stream starts BEFORE the listing, and whatever changed in memory while the listing ran (a
        # write made by this process, an event from the stream) wins over the listing: a big table takes long
        # enough to page that a write landing behind the cursor would otherwise be lost from memory until the
        # next resync -- a blob uploaded during the load answered as "not here".
        self._start_live(pk)
        t0 = time.time() - 5
        started = time.monotonic()
        try:
            docs = await nostr_store.list_all_docs(relay_port(), self.prefix, seckey=sk)
        except Exception as e:      # noqa: BLE001
            raise Unavailable("could not read table %s: %s" % (self.name, e)) from e
        rows = {}
        for d, value in docs.items():
            if isinstance(value, dict):
                rows[sys.intern(urllib.parse.unquote(d[len(self.prefix):]))] = _compact(value)
        with self._lock:
            stamp = {}                  # a row loaded here is stamp 0 (`.get(k, 0)`): no entry per row
            for k, at in self._stamp.items():
                if at >= t0:
                    stamp[k] = at
                    if k in self._rows:
                        rows[k] = self._rows[k]
                    else:
                        rows.pop(k, None)
            self._rows = rows
            self._stamp = stamp
            self._loaded_at = time.time()
        self.load_seconds = round(time.monotonic() - started, 3)
        self._notify(None, None)

    def load(self, *, force: bool = False) -> None:
        with self._load_lock:
            if self._loaded_at and not force:
                return
            _run(self.aload(force=force))

    def start_background_load(self) -> None:
        """Load this table on a THREAD of its own and keep it loaded (the periodic backstop reload, and a reload
        after the live stream had a gap, happen there too -- never inside a request). Before the table's marker
        exists there is nothing to load: SQL answers, and the thread waits for the marker. Idempotent."""
        with self._bg_lock:             # never _load_lock: that is held for the whole of a running load
            if self._bg is not None and self._bg.is_alive():
                return
            th = threading.Thread(target=self._bg_loop, name="doctable-load-%s" % self.name, daemon=True)
            self._bg = th
            th.start()

    def _orphaned(self) -> bool:
        """This instance is no longer the table's (a test's fresh-process reset): its threads stop."""
        return DocTable._registry.get(self.name) is not self

    def _bg_loop(self) -> None:
        backoff = 1.0
        while not self._orphaned():
            try:
                if (self.legacy is not None or self.name in GATED) and not _run(amarked(self.name)):
                    time.sleep(2.0)
                    continue
                if not self._loaded_at or self._reload_wanted or time.time() - self._loaded_at > self.resync_s:
                    first = not self._loaded_at
                    self._reload_wanted = False
                    self.load(force=True)
                    logger.info("[doctable] %s %sloaded: %d row(s) in %.1fs", self.name, "" if first else "re",
                                len(self._rows), self.load_seconds or 0.0)
                backoff = 1.0
                time.sleep(1.0)
            except Exception as e:      # noqa: BLE001 -- retried; readers meanwhile get Loading / a point read
                logger.warning("[doctable] %s not loaded yet: %s", self.name, e)
                time.sleep(backoff)
                # never loaded: retry soon (the relay is usually just starting -- every second here is a second
                # of whole-table 503s); a reload of a table already held can wait longer
                backoff = min(backoff * 2, 30.0 if self._loaded_at else 3.0)

    def _bg_running(self) -> bool:
        return self._bg is not None and self._bg.is_alive()

    def _loading_error(self) -> Loading:
        return Loading("table %s is still loading" % self.name)

    async def _ensure(self):
        if self._loaded_at:
            if time.time() - self._loaded_at > self.resync_s:
                if self._bg_running():
                    self._reload_wanted = True          # the loader thread reloads; memory stays current meanwhile
                else:
                    try:
                        await self.aload(force=True)
                    except Unavailable:
                        pass            # keep what the live stream kept current; the next read tries again
            return
        if self._bg_running():
            raise self._loading_error()
        await self.aload()

    def _ensure_sync(self):
        if self._loaded_at:
            if time.time() - self._loaded_at > self.resync_s:
                if self._bg_running():
                    self._reload_wanted = True
                elif not _in_loop():
                    try:
                        self.load(force=True)
                    except Unavailable:
                        pass
            return
        if self._bg_running():
            raise self._loading_error()
        if _in_loop():
            raise Unavailable("table %s is not loaded yet" % self.name)
        self.load()

    # ------------------------------------------------------------------ reads
    def _snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._rows.items()}

    def _legacy_rows(self) -> dict:
        return {str(k): _norm(v) for k, v in _sql(self.legacy.rows).items()}

    def _legacy_get(self, k):
        row = _sql(lambda db: self.legacy.get(db, str(k)))
        return _norm(row) if row is not None else None

    def all(self) -> dict:
        """{key: row} for the whole table. Raises Unavailable until a strict load has succeeded (Loading while a
        background load runs); before the marker, the rows are SQL's."""
        if not self.migrated():
            return self._legacy_rows()
        self._ensure_sync()
        return self._snapshot()

    async def aall(self) -> dict:
        if not await self.amigrated():
            return await asyncio.to_thread(self._legacy_rows)
        await self._ensure()
        return self._snapshot()

    def get(self, k):
        """One row by key (a copy), or None. While a background load runs, read from the relay (off the loop)."""
        if not self.migrated():
            return self._legacy_get(k)
        if not self._loaded_at and self._bg_running():
            if _in_loop():
                raise self._loading_error()
            return _run(self.aget_remote(k))
        self._ensure_sync()
        with self._lock:
            row = self._rows.get(str(k))
            return dict(row) if row is not None else None

    async def aget(self, k):
        if not await self.amigrated():
            return await asyncio.to_thread(self._legacy_get, k)
        if not self._loaded_at and self._bg_running():
            return await self.aget_remote(k)
        await self._ensure()
        with self._lock:
            row = self._rows.get(str(k))
            return dict(row) if row is not None else None

    def where(self, pred) -> list:
        return [(k, r) for k, r in self.all().items() if pred(r)]

    async def awhere(self, pred) -> list:
        return [(k, r) for k, r in (await self.aall()).items() if pred(r)]

    def view(self) -> list:
        """[(key, row)] of the whole table WITHOUT copying the rows when they come from memory (read them, never
        mutate them). Before the marker: SQL's rows. Synchronous code off the loop loads the table if it must; on
        a loop an unloaded table is Unavailable rather than a blocked loop."""
        if not self.migrated():
            return list(self._legacy_rows().items())
        self._ensure_sync()
        with self._lock:
            return list(self._rows.items())

    async def aview(self) -> list:
        if not await self.amigrated():
            return list((await asyncio.to_thread(self._legacy_rows)).items())
        await self._ensure()
        with self._lock:
            return list(self._rows.items())

    def rows_view(self) -> list:
        """[(key, row)] WITHOUT copying the rows -- for a scan over a big table, where `all()` would copy every
        row. The rows are this table's own memory: read them, never mutate them. Unavailable until loaded."""
        if not self._loaded_at:
            raise (self._loading_error() if self._bg_running() else
                   Unavailable("table %s is not loaded" % self.name))
        with self._lock:
            return list(self._rows.items())

    def peek(self, k):
        """The row as held in memory (not a copy -- read only), or None. Unavailable until loaded."""
        if not self._loaded_at:
            raise (self._loading_error() if self._bg_running() else
                   Unavailable("table %s is not loaded" % self.name))
        with self._lock:
            return self._rows.get(str(k))

    async def aget_remote(self, k):
        """ONE row read straight from the relay, strictly -- for a point lookup before the table is loaded.
        None means the relay answered and holds no such row; a relay that could not be asked raises."""
        sk, _pk = _operator()
        try:
            value = await nostr_store.get_doc(relay_port(), self.prefix + key(k), seckey=sk, strict=True)
        except Exception as e:      # noqa: BLE001
            raise Unavailable("could not read %s/%s: %s" % (self.name, k, e)) from e
        return value if isinstance(value, dict) else None

    # ------------------------------------------------------------------ writes
    # After the marker: relay first, memory after. Before it: SQL first (the store of record), then the relay.
    async def _relay_put(self, k, payload: dict) -> None:
        sk, _pk = _operator()
        ok = await nostr_store.put_doc(relay_port(), sk, self.prefix + key(k), payload)
        if not ok:
            raise Unavailable("the relay did not store %s/%s" % (self.name, k))
        with self._lock:
            self._rows[str(k)] = payload
            self._stamp[str(k)] = int(time.time())
        self._notify(str(k), payload)

    async def _relay_delete(self, k) -> None:
        sk, _pk = _operator()
        try:
            ok = await nostr_store.delete_doc(relay_port(), sk, self.prefix + key(k), strict=True)
        except Exception as e:      # noqa: BLE001 -- "could not ask" is not "already gone"
            raise Unavailable("could not delete %s/%s: %s" % (self.name, k, e)) from e
        if not ok:
            raise Unavailable("the relay did not delete %s/%s" % (self.name, k))
        with self._lock:
            self._rows.pop(str(k), None)
            self._stamp[str(k)] = int(time.time())
        self._notify(str(k), None)

    async def _relay_half(self, coro, what: str) -> None:
        """The relay half of a write SQL has already committed. SQL is the store of record until the marker, so a
        relay that refused is not a failed write (the copy carries the row across) -- unless the marker was
        written meanwhile, in which case SQL is no longer read and the write did not happen as far as anyone
        will ever see: that, or a marker that cannot be asked about, raises."""
        try:
            await coro
        except Unavailable as e:
            try:
                now_marked = await amarked(self.name, fresh=True)
            except Unavailable:
                now_marked = True
            if now_marked:
                raise
            logger.warning("[doctable] %s %s: SQL has it, the relay did not take it yet (%s) -- the migration "
                           "copies it", self.name, what, e)

    async def aput(self, k, row: dict) -> None:
        if not isinstance(row, dict):
            raise TypeError("a DocTable row is a dict")
        payload = _compact(_norm(row))         # what is stored is exactly what is cached
        if not await self.amigrated():
            await asyncio.to_thread(_sql, lambda db: self.legacy.put(db, str(k), payload), write=True)
            await self._relay_half(self._relay_put(k, payload), "write %s" % k)
            return
        await self._relay_put(k, payload)

    def put(self, k, row: dict) -> None:
        _run(self.aput(k, row))

    async def ainsert(self, row: dict, mint) -> str:
        """Store a NEW row and return its key. Before the marker an `int_ids` table takes SQL's own id -- the
        Legacy writes it into the row's "id" field, and the relay copy carries that same id; after the marker the
        key is `mint()`, and a row whose "id" is None gets the key as its id."""
        if not isinstance(row, dict):
            raise TypeError("a DocTable row is a dict")
        if self.legacy is not None and self.legacy.int_ids and not await self.amigrated():
            payload = _norm(row)

            def ins(db):
                return str(self.legacy.insert(db, payload))
            k = await asyncio.to_thread(_sql, ins, write=True)
            payload = _compact(payload)
            await self._relay_half(self._relay_put(k, payload), "insert %s" % k)
            return k
        k = mint()
        if asyncio.iscoroutine(k):
            k = await k
        k = str(k)
        if "id" in row and row.get("id") is None:
            row = dict(row, id=int(k))
        await self.aput(k, row)
        return k

    def insert(self, row: dict, mint) -> str:
        return _run(self.ainsert(row, mint))

    async def adelete(self, k) -> None:
        if not await self.amigrated():
            await asyncio.to_thread(_sql, lambda db: self.legacy.delete(db, str(k)), write=True)
            await self._relay_half(self._relay_delete(k), "delete %s" % k)
            return
        await self._relay_delete(k)

    def delete(self, k) -> None:
        _run(self.adelete(k))

    # ------------------------------------------------------------------ live stream (other processes' writes)
    def _apply_event(self, ev: dict, sk: bytes) -> None:
        kind = int(ev.get("kind", -1))
        at = int(ev.get("created_at", 0))
        if kind == KIND:
            d = next((t[1] for t in ev.get("tags") or [] if len(t) >= 2 and t[0] == "d"), "")
            if not d.startswith(self.prefix):
                return
            k = urllib.parse.unquote(d[len(self.prefix):])
            with self._lock:
                if at < self._stamp.get(k, 0):
                    return
            try:
                value = nostr_store._decode(ev.get("content", ""), sk, True)
            except Exception:       # noqa: BLE001 -- one unreadable document is one row left alone
                return
            if isinstance(value, dict):
                value = _compact(value)
                with self._lock:
                    self._rows[k] = value
                    self._stamp[k] = at
                self._notify(k, value)
        elif kind == 5:
            for t in ev.get("tags") or []:
                if len(t) >= 2 and t[0] == "a":
                    parts = str(t[1]).split(":", 2)
                    if len(parts) == 3 and parts[2].startswith(self.prefix):
                        k = urllib.parse.unquote(parts[2][len(self.prefix):])
                        with self._lock:
                            if at < self._stamp.get(k, 0):
                                continue
                            self._rows.pop(k, None)
                            self._stamp[k] = at
                        self._notify(k, None)

    def _start_live(self, pk: str) -> None:
        if self._live is not None and self._live.is_alive():
            return
        sk, _ = _operator()
        t = threading.Thread(target=self._live_loop, args=(sk, pk), name="doctable-%s" % self.name, daemon=True)
        self._live = t
        t.start()

    def _live_loop(self, sk: bytes, pk: str) -> None:
        from websockets.sync.client import connect
        from app.services.relay_reader import _authenticate
        backoff = 1.0
        dropped_at = None
        while not self._orphaned():
            # A RECONNECT asks from where the last stream dropped, so the relay replays what was written in the
            # gap (stored events, deletions included) -- otherwise only the periodic full reload would find
            # them, and on a big table that reload is the expensive thing.
            since = int(dropped_at if dropped_at is not None else time.time()) - 5
            try:
                url = "ws://127.0.0.1:%d/relay" % relay_port()
                with connect(url, open_timeout=10, close_timeout=2, max_size=16 * 1024 * 1024) as ws:
                    _authenticate(ws, url, sk, 10)        # 30078 is served live only to its signed-in author
                    sub = "dt-" + self.name[:20]
                    ws.send(json.dumps(["REQ", sub,
                                        {"authors": [pk], "kinds": [KIND], "#d~": [self.prefix], "since": since},
                                        {"authors": [pk], "kinds": [5], "since": since}]))
                    backoff = 1.0
                    while True:
                        try:
                            msg = json.loads(ws.recv(timeout=120))
                        except TimeoutError:
                            continue
                        if msg[0] == "EVENT" and len(msg) >= 3:
                            self._apply_event(msg[2], sk)
                        elif msg[0] == "EOSE":
                            dropped_at = None           # the gap has been replayed
                        elif msg[0] == "CLOSED":
                            raise Unavailable("live subscription closed: %s" % (msg[2:] or ""))
            except Exception as e:      # noqa: BLE001 -- the stream reconnects; the periodic reload backstops it
                logger.debug("[doctable] %s live stream: %s", self.name, e)
                if dropped_at is None:
                    dropped_at = time.time()
                # a gap in the stream may have missed writes: reload strictly -- on the loader thread when this
                # process runs one (a big table must never reload inside a request), else on the next read
                if self._bg_running():
                    self._reload_wanted = True
                else:
                    with self._lock:
                        if self._loaded_at:
                            self._loaded_at = time.time() - self.resync_s - 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
