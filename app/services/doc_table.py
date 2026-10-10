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

    @property
    def loaded(self) -> bool:
        """True once a strict load has succeeded (reads are then answered from memory)."""
        return bool(self._loaded_at)

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
        self._notify(None, None)

    def load(self, *, force: bool = False) -> None:
        with self._load_lock:
            if self._loaded_at and not force:
                return
            _run(self.aload(force=force))

    async def _ensure(self):
        if not self._loaded_at:
            await self.aload()
        elif time.time() - self._loaded_at > _RESYNC_S:
            try:
                await self.aload(force=True)
            except Unavailable:
                pass                    # keep what the live stream kept current; the next read tries again

    def _ensure_sync(self):
        if not self._loaded_at:
            self.load()
        elif time.time() - self._loaded_at > _RESYNC_S and not _in_loop():
            try:
                self.load(force=True)
            except Unavailable:
                pass

    # ------------------------------------------------------------------ reads
    def _snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._rows.items()}

    def all(self) -> dict:
        """{key: row} for the whole table. Raises Unavailable until a strict load has succeeded."""
        self._ensure_sync()
        return self._snapshot()

    async def aall(self) -> dict:
        await self._ensure()
        return self._snapshot()

    def get(self, k):
        self._ensure_sync()
        with self._lock:
            row = self._rows.get(str(k))
            return dict(row) if row is not None else None

    async def aget(self, k):
        await self._ensure()
        with self._lock:
            row = self._rows.get(str(k))
            return dict(row) if row is not None else None

    def where(self, pred) -> list:
        return [(k, r) for k, r in self.all().items() if pred(r)]

    async def awhere(self, pred) -> list:
        return [(k, r) for k, r in (await self.aall()).items() if pred(r)]

    def rows_view(self) -> list:
        """[(key, row)] WITHOUT copying the rows -- for a scan over a big table, where `all()` would copy every
        row. The rows are this table's own memory: read them, never mutate them. Unavailable until loaded."""
        if not self._loaded_at:
            raise Unavailable("table %s is not loaded" % self.name)
        with self._lock:
            return list(self._rows.items())

    def peek(self, k):
        """The row as held in memory (not a copy -- read only), or None. Unavailable until loaded."""
        if not self._loaded_at:
            raise Unavailable("table %s is not loaded" % self.name)
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

    # ------------------------------------------------------------------ writes (relay first, memory after)
    async def aput(self, k, row: dict) -> None:
        if not isinstance(row, dict):
            raise TypeError("a DocTable row is a dict")
        sk, _pk = _operator()
        payload = _compact(json.loads(json.dumps(row, default=str)))   # what is stored is exactly what is cached
        ok = await nostr_store.put_doc(relay_port(), sk, self.prefix + key(k), payload)
        if not ok:
            raise Unavailable("the relay did not store %s/%s" % (self.name, k))
        with self._lock:
            self._rows[str(k)] = payload
            self._stamp[str(k)] = int(time.time())
        self._notify(str(k), payload)

    def put(self, k, row: dict) -> None:
        _run(self.aput(k, row))

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
        while True:
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
                # a gap in the stream may have missed writes: make the next read reload strictly
                with self._lock:
                    if self._loaded_at:
                        self._loaded_at = time.time() - _RESYNC_S - 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
