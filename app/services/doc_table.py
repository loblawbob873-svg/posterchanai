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

RELAY-ONLY (#161, owner's call: "the entire point of this was to avoid postgres"). There is no SQL side, no
marker gating and no dual write: a node with no database at all serves every table from the first request. The
rows a node held in Postgres before this are copied ONCE by scripts/migrate_tables_to_nostr.py (read-only SQL,
verified row for row) -- the only code that still imports those SQLAlchemy models.

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
import atexit
import json
import logging
import os
import sys
import zlib
import threading
import time
import urllib.parse

from app.services import nostr_store
from app.services.relay_reader import Unavailable, relay_port

logger = logging.getLogger(__name__)

NS = "pcai:t:"
KIND = nostr_store.APP_KIND
# NO periodic reload. A table is loaded ONCE per process and kept current by its live subscription, which replays
# every gap from where it dropped (deletions included) -- the way a database replica follows its log. A timed full
# reload re-read and re-decrypted every row of every table (the 388k-row fedi ledger, the 190k-row blob index) every
# ten minutes in both the app and the worker (2026-10-10). `resync_s` stays settable for a caller that wants one.
_RESYNC_S = None


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


# ====================================================================== restart snapshots
# A RESTART RE-READ EVERY TABLE IN FULL -- the 190k-row blob index, the 165k-row fediverse ledger, the puppets: ~380,000
# documents through the relay in each process on every deploy (2026-10-18), the relay's largest burst. A table now
# leaves an ENCRYPTED snapshot of its rows when the process stops cleanly with its live stream current, and the next
# start loads it and reads from the relay only what changed since -- documents written AND deletions, both in full
# pages (an unlimited REQ is capped at 500 by the relay, so the live replay cannot be the delta). Anything doubtful is
# the old full load: no snapshot, unreadable or tampered, older than _SNAP_MAX_AGE, another table/namespace/key, or a
# delta that could not be read to its end. Written only at a clean stop: nothing extra hits the SSD in steady state.
_SNAP_MAGIC = b"PCDTS1"
_SNAP_MAX_AGE = 3 * 86400
_SNAP_MARGIN = 300              # the snapshot claims "complete up to now - margin": the live stream's lag, with room


def _snapshot_dir() -> str:
    d = os.environ.get("PC_DOCTABLE_SNAPSHOT_DIR")
    if d is not None:
        return d                                       # "" turns snapshots off
    if "pytest" in sys.modules:
        return ""                                      # tests never write the working tree's state
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "data", "doctable")


def _snapshot_cipher(sk: bytes):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"pcai doctable snapshot v1").derive(sk)
    return AESGCM(key)


def _write_snapshot(path: str, name: str, sk: bytes, payload: dict) -> int:
    raw = zlib.compress(json.dumps(payload, separators=(",", ":"), default=str).encode(), 3)
    nonce = os.urandom(12)
    blob = _SNAP_MAGIC + nonce + _snapshot_cipher(sk).encrypt(nonce, raw, name.encode())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    return len(blob)


def _read_snapshot(path: str, name: str, sk: bytes):
    with open(path, "rb") as f:
        blob = f.read()
    if not blob.startswith(_SNAP_MAGIC):
        raise ValueError("not a table snapshot")
    nonce, ct = blob[len(_SNAP_MAGIC):len(_SNAP_MAGIC) + 12], blob[len(_SNAP_MAGIC) + 12:]
    return json.loads(zlib.decompress(_snapshot_cipher(sk).decrypt(nonce, ct, name.encode())))


async def _deletions_since(sk: bytes, pk: str, since: int, page: int = 2000) -> dict:
    """{d_tag: created_at of the newest deletion} for every kind-5 the operator signed since `since` -- in full."""
    out: dict = {}
    cursor = None
    while True:
        flt = {"authors": [pk], "kinds": [5], "since": int(since), "limit": page}
        if cursor:
            flt["_cursor"] = [int(cursor[0]), str(cursor[1])]
        evs = await nostr_store._ws_query(relay_port(), [flt], strict=True, auth_seckey=sk)
        for ev in evs:
            at = int(ev.get("created_at") or 0)
            for t in ev.get("tags") or []:
                if len(t) >= 2 and t[0] == "a":
                    parts = str(t[1]).split(":", 2)
                    if len(parts) == 3 and parts[1] == pk:
                        out[parts[2]] = max(out.get(parts[2], 0), at)
        if len(evs) < page:
            return out
        nxt = min((int(e.get("created_at") or 0), str(e.get("id") or "")) for e in evs)
        if cursor is not None and nxt >= cursor:
            raise nostr_store.IncompleteRead("deletions since %d could not be read to their end" % since)
        cursor = nxt


async def _docs_since(sk: bytes, prefix: str, since: int, page: int = 1000) -> dict:
    """{d_tag: (value, created_at)} for every document under `prefix` written since `since` -- in full."""
    out: dict = {}
    cursor = None
    while True:
        docs = await nostr_store.list_docs(relay_port(), prefix, seckey=sk, strict=True, limit=page,
                                           with_meta="cursor", cursor=cursor, since=int(since))
        for d, (value, at, _eid) in docs.items():
            if d not in out or at > out[d][1]:
                out[d] = (value, at)
        if len(docs) < page:
            return out
        nxt = min((at, eid) for _v, at, eid in docs.values())
        if cursor is not None and nxt >= cursor:
            raise nostr_store.IncompleteRead("%s since %d could not be read to its end" % (prefix, since))
        cursor = nxt


class Loading(Unavailable):
    """The table is being loaded in the background: ask again in `retry_after` seconds. Whole-table reads only --
    a point read is answered from the relay meanwhile."""

    def __init__(self, msg: str, retry_after: int = 10):
        super().__init__(msg)
        self.retry_after = retry_after


def _norm(row: dict) -> dict:
    """Exactly what a document holds for `row` (its JSON round trip)."""
    return json.loads(json.dumps(row, default=str))


# ====================================================================== migration markers (the copy script's record)
# The one-time copy of a node's old Postgres rows (scripts/migrate_tables_to_nostr.py) writes a `_migrated` row per
# table when it has verified the copy. NOTHING at runtime reads it: every table is relay-only from the first
# request, on a fresh node and on a migrated one alike.
MARKERS = "_migrated"


def is_marker(m) -> bool:
    return isinstance(m, dict) and (m.get("ok") is True or m.get("verified") is True)


def reset_state() -> None:
    """Tests: a fresh process (nothing is held outside the table registry any more)."""


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
        self._bg = None                 # the background loader thread, when this process runs one
        self._bg_lock = threading.Lock()
        self._reload_wanted = False
        self.load_seconds = None        # how long the last strict load took (measured, reported)
        self.resync_s = _RESYNC_S       # None: never reload (the live stream keeps the table current)
        self._live_ok = False           # the live stream is connected and has replayed its gap (EOSE)
        self.loaded_from = None         # "snapshot" or "relay": how the last load was answered (reported)

    @property
    def loaded(self) -> bool:
        """True once a strict load has succeeded (reads are then answered from memory)."""
        return bool(self._loaded_at)

    @property
    def loading(self) -> bool:
        """A background load is running and has not finished its first pass."""
        return not self._loaded_at and self._bg is not None and self._bg.is_alive()

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
        rows = None
        if not self._loaded_at:
            try:
                rows = await self._from_snapshot(sk, pk)
            except Exception as e:      # noqa: BLE001 -- any doubt is the full load below
                logger.info("[doctable] %s snapshot not used: %s", self.name, e)
                rows = None
        if rows is None:
            try:
                docs = await nostr_store.list_all_docs(relay_port(), self.prefix, seckey=sk)
            except Exception as e:      # noqa: BLE001
                raise Unavailable("could not read table %s: %s" % (self.name, e)) from e
            rows = {}
            for d, value in docs.items():
                if isinstance(value, dict):
                    rows[sys.intern(urllib.parse.unquote(d[len(self.prefix):]))] = _compact(value)
            self.loaded_from = "relay"
        else:
            self.loaded_from = "snapshot"
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

    def _snapshot_path(self) -> str:
        d = _snapshot_dir()
        return os.path.join(d, self.name + ".snap") if d else ""

    async def _from_snapshot(self, sk: bytes, pk: str):
        """The rows of the last clean stop brought up to date from the relay, or None (use the full load)."""
        path = self._snapshot_path()
        if not path or not os.path.exists(path):
            return None
        snap = _read_snapshot(path, self.name, sk)
        since = int(snap.get("since") or 0)
        if snap.get("v") != 1 or snap.get("ns") != NS or snap.get("name") != self.name or snap.get("pk") != pk:
            raise ValueError("made for another table or key")
        if since <= time.time() - _SNAP_MAX_AGE:
            raise ValueError("older than %d days" % (_SNAP_MAX_AGE // 86400))
        rows = {sys.intern(k): _compact(v) for k, v in (snap.get("rows") or {}).items() if isinstance(v, dict)}
        changed = await _docs_since(sk, self.prefix, since - 5)
        deleted = await _deletions_since(sk, pk, since - 5)
        n_changed = n_deleted = 0
        for d, (value, at) in changed.items():
            k = sys.intern(urllib.parse.unquote(d[len(self.prefix):]))
            if isinstance(value, dict) and at > deleted.get(d, -1):
                rows[k] = _compact(value)
                n_changed += 1
        for d in deleted:
            if d.startswith(self.prefix) and not (d in changed and changed[d][1] > deleted[d]):
                if rows.pop(urllib.parse.unquote(d[len(self.prefix):]), None) is not None:
                    n_deleted += 1
        logger.info("[doctable] %s: %d row(s) from its snapshot, %d changed and %d deleted since", self.name,
                    len(rows), n_changed, n_deleted)
        return rows

    def save_snapshot(self) -> bool:
        """Write this table's snapshot if it can vouch for it: loaded, and its live stream current. Clean stop only."""
        path = self._snapshot_path()
        if not path or not self._loaded_at or not self._live_ok:
            return False
        try:
            sk, pk = _operator()
            with self._lock:
                rows = dict(self._rows)
            n = _write_snapshot(path, self.name, sk, {"v": 1, "ns": NS, "name": self.name, "pk": pk,
                                                      "since": int(time.time()) - _SNAP_MARGIN, "rows": rows})
            logger.info("[doctable] %s snapshot saved: %d row(s), %d bytes", self.name, len(rows), n)
            return True
        except Exception as e:      # noqa: BLE001 -- a missing snapshot only means a full load next time
            logger.warning("[doctable] %s snapshot not saved: %s", self.name, e)
            return False

    def load(self, *, force: bool = False) -> None:
        with self._load_lock:
            if self._loaded_at and not force:
                return
            _run(self.aload(force=force))

    def start_background_load(self) -> None:
        """Load this table on a THREAD of its own and keep it loaded (the periodic backstop reload, and a reload
        after the live stream had a gap, happen there too -- never inside a request). Before the table's marker
        Idempotent."""
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
                if not self._loaded_at or self._reload_wanted or self._stale():
                    first = not self._loaded_at
                    self._reload_wanted = False
                    self.load(force=True)
                    logger.info("[doctable] %s %sloaded: %d row(s) in %.1fs", self.name, "" if first else "re",
                                len(self._rows), self.load_seconds or 0.0)
                backoff = 1.0
                time.sleep(1.0)
            except (Exception, asyncio.CancelledError) as e:  # noqa: BLE001 -- retried; readers get Loading meanwhile
                # CancelledError is a BaseException: a relay read cancelled by a timeout inside the load must not
                # end this thread (that would leave the table unloaded for good, with nothing left to retry it).
                logger.warning("[doctable] %s not loaded yet: %s", self.name, e)
                time.sleep(backoff)
                # never loaded: retry soon (the relay is usually just starting -- every second here is a second
                # of whole-table 503s); a reload of a table already held can wait longer
                backoff = min(backoff * 2, 30.0 if self._loaded_at else 3.0)

    def _stale(self) -> bool:
        return bool(self.resync_s) and time.time() - self._loaded_at > self.resync_s

    def _bg_running(self) -> bool:
        return self._bg is not None and self._bg.is_alive()

    def _loading_error(self) -> Loading:
        return Loading("table %s is still loading" % self.name)

    async def _ensure(self):
        if self._loaded_at:
            if self._stale():
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
        try:
            await self.aload()
        except BaseException:
            # A failed first load must not be the last attempt: the loader thread retries it with backoff until it
            # succeeds, so a caller that never asks again (a scheduler, a process that ran no startup loader) still
            # ends up with the table.
            self.start_background_load()
            raise

    def _ensure_sync(self):
        if self._loaded_at:
            if self._stale():
                if self._bg_running():
                    self._reload_wanted = True
                elif not _in_loop():
                    try:
                        self.load(force=True)
                    except Unavailable:
                        pass
            return
        if self._bg_running():
            raise self._loading_error()     # a big table loading on its thread: 503 + Retry-After, not a wait
        if _in_loop():
            self.start_background_load()
            raise self._loading_error()
        try:
            self.load()
        except BaseException:
            self.start_background_load()        # retried on the loader thread (see _ensure)
            raise

    # ------------------------------------------------------------------ reads
    def _snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._rows.items()}

    def all(self) -> dict:
        """{key: row} for the whole table. Raises Unavailable until a strict load has succeeded (Loading while a
        background load runs)."""
        self._ensure_sync()
        return self._snapshot()

    async def aall(self) -> dict:
        await self._ensure()
        return self._snapshot()

    def get(self, k):
        """One row by key (a copy), or None. Before the table is loaded: that ONE document from the relay (off the
        loop; on a loop it is Loading -- a socket is never awaited there), and the table loads on its own thread."""
        if not self._loaded_at:
            if _in_loop():
                self.start_background_load()
                raise self._loading_error()
            return _run(self.aget_remote(k))
        with self._lock:
            row = self._rows.get(str(k))
            return dict(row) if row is not None else None

    async def aget(self, k):
        """One row by key (a copy), or None. Before the table is loaded: that ONE document from the relay -- a
        restart's load never turns a point lookup into a wait or a 503."""
        if not self._loaded_at:
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
        mutate them). Synchronous code off the loop loads the table if it must; on a loop an unloaded table is
        Unavailable rather than a blocked loop."""
        self._ensure_sync()
        with self._lock:
            return list(self._rows.items())

    async def aview(self) -> list:
        await self._ensure()
        with self._lock:
            return list(self._rows.items())

    def rows_view(self) -> list:
        """[(key, row)] WITHOUT copying the rows -- for a scan over a big table, where `all()` would copy every
        row. The rows are this table's own memory: read them, never mutate them. Unavailable until loaded -- and
        the table starts loading on its own thread, so the retry the caller is told to make finds it loaded (a
        process that never ran the startup loader, the worker, would otherwise answer 503 for ever)."""
        if not self._loaded_at:
            self.start_background_load()
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

    async def aget_many(self, keys) -> dict:
        """{key: row (a copy)} for the rows among `keys` that exist. Before the table is loaded: those documents read
        from the relay in one strict bulk read -- a missing key is simply absent; "could not ask" raises."""
        ks = [str(k) for k in dict.fromkeys(keys or []) if k is not None and str(k) != ""]
        if not ks:
            return {}
        if not self._loaded_at:
            return await self.aget_many_remote(ks)
        await self._ensure()
        with self._lock:
            return {k: dict(self._rows[k]) for k in ks if k in self._rows}

    async def aget_many_remote(self, keys) -> dict:
        """`aget_remote` for many keys at once (one REQ per 200), strictly."""
        ks = [str(k) for k in dict.fromkeys(keys or [])]
        if not ks:
            return {}
        sk, _pk = _operator()
        tags = {self.prefix + key(k): k for k in ks}
        try:
            docs = await nostr_store.get_docs(relay_port(), list(tags), seckey=sk, strict=True)
        except Exception as e:      # noqa: BLE001
            raise Unavailable("could not read %d row(s) of %s: %s" % (len(ks), self.name, e)) from e
        return {tags[d]: v for d, v in docs.items() if d in tags and isinstance(v, dict)}

    # ------------------------------------------------------------------ writes (relay first, memory after)
    async def aput(self, k, row: dict) -> None:
        if not isinstance(row, dict):
            raise TypeError("a DocTable row is a dict")
        payload = _compact(_norm(row))         # what is stored is exactly what is cached
        sk, _pk = _operator()
        ok = await nostr_store.put_doc(relay_port(), sk, self.prefix + key(k), payload)
        if not ok:
            raise Unavailable("the relay did not store %s/%s" % (self.name, k))
        with self._lock:
            self._rows[str(k)] = payload
            self._stamp[str(k)] = int(time.time())
        self._notify(str(k), payload)

    def put(self, k, row: dict) -> None:
        _run(self.aput(k, row))

    async def ainsert(self, row: dict, mint) -> str:
        """Store a NEW row under the key `mint()` returns (a row whose "id" is None gets the key as its id)."""
        if not isinstance(row, dict):
            raise TypeError("a DocTable row is a dict")
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
                with connect(url, open_timeout=10, close_timeout=2, max_size=16 * 1024 * 1024,
                             compression=None) as ws:
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
                            self._live_ok = True
                        elif msg[0] == "CLOSED":
                            raise Unavailable("live subscription closed: %s" % (msg[2:] or ""))
            except Exception as e:      # noqa: BLE001 -- the stream reconnects; the periodic reload backstops it
                self._live_ok = False
                logger.debug("[doctable] %s live stream: %s", self.name, e)
                if dropped_at is None:
                    dropped_at = time.time()
                # No reload for a gap: the reconnect asks from `dropped_at`, and the relay replays what was written
                # meanwhile, deletions included. A full reload per dropped socket re-read whole tables every time the
                # relay restarted.
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def save_snapshots() -> int:
    """Every loaded table's snapshot -- at a clean stop (registered with atexit; callable from a shutdown hook)."""
    return sum(1 for t in list(DocTable._registry.values()) if t.save_snapshot())


atexit.register(save_snapshots)
