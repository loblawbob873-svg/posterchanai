"""PosterChanOS's LOCAL RELAY: the desktop's event store (`relay_main.py --os-mode`).

WHY. Every PosterChanOS window is a Chromium renderer with its own copy of the client's event cache, and
that cache lives in IndexedDB, i.e. LevelDB. Measured on the two machines in use on 2026-10-12, LevelDB
rewrote the same ~180 MB of cache over and over in compactions (a 3.4 MB level-0 flush every few minutes
turning into 20-30 MB of compaction writes, and bursts of 400-500 MB a minute while somebody scrolled),
and the renderers held 1-2.5 GB each. This process is one append-only PosterChanDB store for every window
of the session, so a cached event is written ONCE, sequentially, and never rewritten by a compaction.

WHAT IT IS NOT. It is the same NIP-01 relay code as a server node (`server.RelayServer` over
`pcdb_store.PcdbRelayStore`) with every server job taken away, by construction rather than by settings:
  * it listens on LOOPBACK ONLY (anything else is refused at start);
  * its ONLY upstream is the instance chosen on the desktop's welcome screen (`config.json` -> `instance`),
    read at every pass so a change there takes effect without a restart; with none chosen it is a pure
    local cache, and with the desktop's Tor switched on it does not dial out at all (it has no way into the
    desktop's Tor, and dialling the instance directly would put this machine's address beside a session
    that is hiding it);
  * NO web-of-trust crawl, no firehose, no other relays, no outbox, no ActivityPub, no NIP-05 server, no
    paid retention, no settings store, no Postgres. None of those modules is even imported.

WHAT IT SYNCS (InstanceSync), using NIP-77 negentropy where the instance supports it and a `since` REQ
where it does not: the signed-in user's own events, their private pcai documents (kind 30078 -- the
instance only serves those to a socket authenticated AS the user, and this process holds no key, so the
AUTH is signed by the user's own client over the loopback socket: `PC-SIGN-AUTH`, answered only for the
instance's relay URL), what the people they follow posted lately, and what mentions or DMs them. It only
DOWNLOADS: the client still publishes every write to the instance and the user's relays itself, so a cache
can never become the reason a post went somewhere.

WHO THE USER IS comes from the client: the latest NIP-42 AUTH names the owner (an account switch moves the scope; the owner only scopes what is FETCHED -- every read is still gated per pubkey). Until a
client has authenticated there is nothing to scope a sync to, so nothing is fetched.

EVICTION is least-recently-USED under a byte cap, never "the newest N": a profile somebody opens every
day must outlive a burst of the global feed. The user's private documents are pinned by the SAME rule as
the client's own cache (os_pins.py == store.js `_isPinned`), so the cap can never cost a note, the vault
or the drive index.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import signal
import threading
import time
import urllib.parse
import urllib.request

import numpy as np

from websockets.datastructures import Headers
from websockets.http11 import Response

from app.services.nostr.event import verify_event
from . import negentropy, os_pins
from .pcdb_store import PcdbRelayStore, init_new, read_marker
from .server import RelayServer, _matches, _one_header
from .wot import WotGate

logger = logging.getLogger("posterchan-relay")

MB = 1024 * 1024
LOOPBACK = "127.0.0.1"

# What the cache keeps of the user's own history (every kind a screen in the client reads; the private
# 30078 documents are their own filter, because the instance only answers that one after AUTH).
OWN_KINDS = [0, 1, 3, 5, 6, 7, 16, 20, 21, 22, 1018, 1068, 1111, 1984, 9735, 10000, 10001, 10002,
             10003, 10050, 30023]
FOLLOW_KINDS = [0, 1, 6, 16, 20, 21, 22, 1068, 30023]
MENTION_KINDS = [1, 6, 7, 16, 1111, 9735]
FOLLOWS_MAX = 2000           # authors synced (the newest-following first is not knowable; the list order is)
AUTHORS_PER_FILTER = 250
FOLLOW_WINDOW = 7 * 86400
MENTION_WINDOW = 14 * 86400
DM_WINDOW = 30 * 86400
IDS_PER_REQ = 250
NEG_MAX_ITEMS = 50000


# ----------------------------------------------------------------------------------------------- config
def _env_int(env, key, default):
    try:
        return int(str(env.get(key, "")).strip() or default)
    except ValueError:
        return default


def default_port(uid: int | None = None) -> int:
    """One port per Unix account (a second identity signed in on another seat must not collide)."""
    uid = os.getuid() if uid is None else uid
    return 20000 + (uid % 20000)


def _home(env) -> str:
    return env.get("HOME") or os.path.expanduser("~")


def read_config(env=None) -> dict:
    env = os.environ if env is None else env
    home = _home(env)
    data_home = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    runtime = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    bind = (env.get("PC_RELAY_BIND") or LOOPBACK).strip()
    try:
        if not ipaddress.ip_address(bind).is_loopback:
            raise ValueError
    except ValueError:
        # Not a setting with a wrong value: a local cache that answers the network would hand every
        # cached event (DMs included) to whoever can reach this machine.
        raise SystemExit("posterchan-relay: refusing to listen on %r -- the local relay is loopback-only" % bind)
    return {
        "bind": bind,
        "port": _env_int(env, "PC_RELAY_PORT", default_port()),
        "data_dir": env.get("PC_RELAY_DIR") or os.path.join(data_home, "posterchan", "relay"),
        "max_bytes": _env_int(env, "PC_RELAY_MAX_MB", 512) * MB,
        "flush_s": _env_int(env, "PC_RELAY_FLUSH_SECONDS", 30),
        "desktop_config": env.get("PC_DESKTOP_CONFIG") or os.path.join(home, ".config", "posterchan-desktop",
                                                                       "config.json"),
        "ready_file": env.get("PC_RELAY_READY_FILE") or os.path.join(runtime, "posterchan-relay.url"),
        # Tests and an operator's override only; the shipped unit never sets it.
        "upstream": (env.get("PC_RELAY_UPSTREAM") or "").strip(),
        "sync_interval": _env_int(env, "PC_RELAY_SYNC_SECONDS", 120),
        "prune_interval": max(1, _env_int(env, "PC_RELAY_PRUNE_SECONDS", 300)),
    }


def chosen_instance(path: str):
    """(instance URL or '', tor on?) from the desktop's own config.json. An instance that was never CHOSEN
    is no upstream at all -- the desktop falls back to a default for its own use, the cache does not."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except (OSError, ValueError):
        return "", False
    inst = cfg.get("instance")
    inst = str(inst).strip().rstrip("/") if inst is not None else ""
    if inst and not inst.lower().startswith(("http://", "https://")):
        inst = ""
    tor = bool((cfg.get("tor") or {}).get("enabled")) if isinstance(cfg.get("tor"), dict) else False
    return inst, tor


def _ws_url_ok(u: str) -> bool:
    p = urllib.parse.urlparse(u or "")
    return p.scheme in ("ws", "wss") and bool(p.hostname)


def _http_get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "posterchan-relay"})
    # No proxy from the environment: the instance is the one host this process may contact, by name.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read(1 << 20).decode("utf-8"))


def _ws_connect(url: str):
    import websockets
    kw = dict(max_size=8 * MB, open_timeout=15, ping_interval=30, ping_timeout=60, close_timeout=5)
    try:
        # websockets >= 15 reads HTTPS_PROXY/ALL_PROXY by default. The instance is the one host this
        # process may contact, and it contacts it by name.
        return websockets.connect(url, proxy=None, **kw)
    except TypeError:
        return websockets.connect(url, **kw)


# ------------------------------------------------------------------------------------------------ the store
class OsCacheStore(PcdbRelayStore):
    """PosterChanDB as a CACHE: no per-write fsync (the relay's server role fsyncs every direct write before
    it says OK; here every event is a copy of something that lives on the network, and the user's own
    documents are also kept by the client), small segments so a compaction rewrites a few MB rather than
    256, a small read cache, and LRU eviction under a byte cap with the user's documents pinned."""

    LRU_FILE = "lru-used.npy"

    def __init__(self, path: str, *, max_bytes: int = 512 * MB, flush_interval: float = 30.0,
                 segment_bytes: int = 32 * MB, cache_mb: float = 32, log=None):
        super().__init__(path, max_events=0, retention_days=0, flush_interval=flush_interval,
                         cache_mb=cache_mb, log=log or logger.info)
        self.max_bytes = int(max_bytes)
        self.segment_bytes = int(segment_bytes)
        self.owner = ""
        self._used = np.zeros(0, dtype=np.uint32)
        self._used_lock = threading.Lock()
        self._used_dirty = False

    # ------------------------------------------------------------ lifecycle
    def open(self, loop) -> None:
        if read_marker(self.path) is None:
            init_new(self.path)          # a fresh cache directory (refuses one that already holds events)
        super().open(loop)
        self.store.segment_bytes = self.segment_bytes
        self.owner = str((self.sidecar.kv or {}).get("owner_pubkey") or "")
        self._load_used()

    def close(self) -> None:
        try:
            self.save_used()
        except Exception as e:      # noqa: BLE001 -- the recency order is a hint; losing it costs nothing
            logger.warning("[posterchan-relay] could not save the LRU order: %r", e)
        super().close()

    def set_owner(self, pubkey: str) -> None:
        """Called on the event loop by the server's AUTH: the scope changes NOW, and the one small sidecar
        write that remembers it across a restart goes to the writer thread rather than blocking the loop."""
        if pubkey and pubkey != self.owner:
            self.owner = pubkey
            self._write_exec.submit(self._kv_set_sync, "owner_pubkey", pubkey)

    # ------------------------------------------------------------ recency
    def _grow_used(self) -> np.ndarray:
        n = len(self.store.off)
        if len(self._used) < n:
            grown = np.full(max(n, 2 * len(self._used), 1024), int(time.time()), dtype=np.uint32)
            grown[:len(self._used)] = self._used
            self._used = grown
        return self._used

    def _touch(self, seqs) -> None:
        if not len(seqs):
            return
        with self._used_lock:
            used = self._grow_used()
            used[np.asarray(seqs, dtype=np.int64)] = int(time.time())
            self._used_dirty = True

    def _ids_digest(self, n: int) -> str:
        st = self.store
        with st._lock:
            raw = bytes(st.ids[:n * 32])
        return hashlib.sha256(raw).hexdigest()

    def _load_used(self) -> None:
        """The recency order is per SEQ, and a seq is only the same event across a restart while the log
        replays the same records in the same order -- a compaction that dropped dead records renumbers them.
        So the saved order is used only when the ids it was saved against are still the first ids here
        (sha256 of the id column); otherwise the events' own created_at stands in, which costs nothing worse
        than one pass of "newest first" until the next reads re-rank them."""
        st = self.store
        n = len(st.off)
        used = None
        try:
            with open(os.path.join(self.path, self.LRU_FILE + ".json")) as f:
                meta = json.load(f)
            arr = np.load(os.path.join(self.path, self.LRU_FILE), allow_pickle=False)
            m = int(meta.get("n", -1))
            if arr.dtype == np.uint32 and arr.ndim == 1 and len(arr) == m and 0 <= m <= n \
                    and meta.get("ids") == self._ids_digest(m):
                used = arr.copy()
        except (OSError, ValueError, TypeError):
            used = None
        with st._lock:
            created = np.frombuffer(st.created, dtype=np.uint64)[:n].astype(np.int64)
        created = np.clip(created, 0, int(time.time())).astype(np.uint32)
        if used is None:
            used = created
        elif len(used) < n:
            # Rows written since the save are as recent as the newest thing the save knew about.
            tail = np.full(n - len(used), int(used.max()) if len(used) else int(time.time()), dtype=np.uint32)
            used = np.concatenate([used, tail])
        with self._used_lock:
            self._used = used.astype(np.uint32)

    def save_used(self) -> None:
        with self._used_lock:
            if not self._used_dirty or self.store is None:
                return
            n = min(len(self.store.off), len(self._used))
            arr = self._used[:n].copy()
            self._used_dirty = False
        meta = {"n": int(n), "ids": self._ids_digest(n)}
        p = os.path.join(self.path, self.LRU_FILE)
        tmp = p + ".tmp.npy"
        np.save(tmp, arr, allow_pickle=False)
        os.replace(tmp, p)
        with open(p + ".json.tmp", "w") as f:
            json.dump(meta, f)
        os.replace(p + ".json.tmp", p + ".json")

    # ------------------------------------------------------------ writes: no fsync per event
    def _add_event_sync(self, ev: dict, origin: str) -> bool:
        if self._writes_closed:
            return False
        try:
            ok = self._put(ev, origin)
        except Exception as e:      # noqa: BLE001 -- one malformed event costs only itself
            logger.warning("[posterchan-relay] add_event %s failed: %s", str(ev.get("id", ""))[:12], e)
            return False
        self._touch(self._live_seqs_of_fast([ev.get("id")]))
        return ok

    def _add_events_bulk_sync(self, events: list, origin: str) -> int:
        if self._writes_closed:
            return 0
        stored = 0
        for ev in events:
            try:
                if self._put(ev, origin):
                    stored += 1
            except Exception:      # noqa: BLE001
                continue
        self._touch(self._live_seqs_of_fast([e.get("id") for e in events]))
        return stored

    def _live_seqs_of_fast(self, ids) -> list:
        return [s for s in self._live_seqs_of([i for i in ids if isinstance(i, str)]) if s is not None]

    # ------------------------------------------------------------ reads count as use
    def _query_sync(self, filters: list, hard_cap: int) -> list:
        out = super()._query_sync(filters, hard_cap)
        if out:
            self._touch(self._live_seqs_of_fast([e["id"] for e in out]))
        return out

    # ------------------------------------------------------------ eviction
    def _pinned_seqs(self) -> np.ndarray:
        """Seqs the cap never evicts: os_pins' rule, caps included. Columns are COPIED under the store's lock
        and no numpy view of them outlives it: an exported buffer makes the writer's next append raise."""
        st = self.store
        owner = self.owner
        pairs = st.d_prefix_pairs("pcai:")
        with st._lock:
            n = len(st.off)
            kinds = np.frombuffer(st.kind, dtype=np.uint32)[:n].copy()
            dead = np.frombuffer(st.dead, dtype=np.uint8)[:n].copy()
            created = np.frombuffer(st.created, dtype=np.uint64)[:n].astype(np.int64)
            author = np.frombuffer(st.author, dtype=np.uint32)[:n].copy()
            me = st._author_ix.get(owner) if owner else None
            authors = {q: st.pubkey_of(q) for d, q in pairs
                       if q < n and d.startswith(os_pins.D_OWN_PREFIX)}
        pinned = [q for d, q in pairs if q < n and kinds[q] == 30078 and not dead[q]
                  and os_pins.d_pinned(d, authors.get(q, ""), owner)]
        groups = [(np.concatenate([st.tag_seqs("m", m) for m in os_pins.XDC_MIMES]), "xdc")]
        if owner:
            groups.append((st.tag_seqs("p", owner), "notif"))
        for seqs, what in groups:
            seqs = np.unique(np.asarray(seqs, dtype=np.int64))
            seqs = seqs[seqs < n]
            seqs = seqs[dead[seqs] == 0]
            if what == "xdc":
                seqs, cap = seqs[kinds[seqs] == 1063], os_pins.XDC_PIN_MAX
            else:
                seqs = seqs[np.isin(kinds[seqs], np.asarray(sorted(os_pins.NOTIF_KINDS), dtype=np.uint32))]
                if me is not None:
                    seqs = seqs[author[seqs] != me]
                cap = os_pins.NOTIF_PIN_MAX
            order = np.argsort(-created[seqs], kind="stable")
            pinned.extend(int(s) for s in seqs[order[:cap]])
        return np.unique(np.asarray(pinned, dtype=np.int64))

    def lru_victims(self) -> np.ndarray:
        """Least-recently-used live, unpinned seqs to drop until the live bytes fit `max_bytes`."""
        if not self.max_bytes or self.store is None:
            return np.zeros(0, dtype=np.int64)
        st = self.store
        with st._lock:
            n = len(st.off)
            dead = np.frombuffer(st.dead, dtype=np.uint8)[:n].copy()
            length = np.frombuffer(st.length, dtype=np.uint32)[:n].astype(np.int64)
        live = np.nonzero(dead == 0)[0]
        total = int(length[live].sum())
        if total <= self.max_bytes:
            return np.zeros(0, dtype=np.int64)
        cand = np.setdiff1d(live, self._pinned_seqs(), assume_unique=False)
        with self._used_lock:
            used = self._grow_used()[:n].copy()
        order = cand[np.argsort(used[cand], kind="stable")]          # least recently used first
        need = total - self.max_bytes
        cum = np.cumsum(length[order])
        k = int(np.searchsorted(cum, need, side="left")) + 1
        return order[:k]

    def _prune_sync(self, limit: int = 0) -> tuple:
        if self._writes_closed:
            return (0, False)
        now = int(time.time())
        ctx = self._ctx(now)
        removed = self._kill_seqs(self._rule_seqs("expired", ctx))
        victims = self.lru_victims()
        more = False
        if limit and len(victims) > limit:
            victims, more = victims[:limit], True
        removed += self._kill_seqs(victims)
        return removed, more


# ------------------------------------------------------------------------------------------------ the server
class OsRelayServer(RelayServer):
    """RelayServer for one person's loopback cache.

    Two rules differ from a server node and both are about the socket, not the data:
      * a signed kind-30078 is accepted without NIP-42: on a public relay the AUTH rule stops a replayed
        copy of somebody's document being written by a stranger's client, but every event here is a
        signed copy the user's own client is CACHING (it never decides anything), so the signature is the
        proof that matters. READS of 30078 still need AUTH as the author, exactly as on the instance --
        another account on this machine can reach the port;
      * the latest authenticated pubkey is remembered as the OWNER (what the instance sync is scoped to)."""

    def __init__(self, store, gate, config: dict, on_owner=None):
        super().__init__(store, gate, config)
        self._on_owner = on_owner
        self._sign_waiters: dict = {}

    def _nip78_owner(self, conn, pubkey: str) -> bool:
        return True

    def _on_auth(self, conn, ev) -> None:
        before = set(self._auth_pubkeys.get(conn, ()))
        super()._on_auth(conn, ev)
        for pk in self._auth_pubkeys.get(conn, set()) - before:
            if self._on_owner:
                try:
                    self._on_owner(pk)
                except Exception as e:      # noqa: BLE001
                    logger.warning("[posterchan-relay] could not record the owner: %r", e)

    def nip11_doc(self, host: str = "") -> bytes:
        return json.dumps({"name": "PosterChan local relay", "software": "posterchan",
                           "description": "this machine's own event cache (loopback only)",
                           "supported_nips": [1, 9, 11, 40, 42, 77]}).encode()

    def process_request(self, connection, request):
        try:
            hdrs = request.headers
            if _one_header(hdrs, "Upgrade").lower() == "websocket":
                return super().process_request(connection, request)
            accept = _one_header(hdrs, "Accept")
        except Exception:      # noqa: BLE001
            return None
        if "application/nostr+json" in accept:
            return Response(200, "OK", Headers({"Content-Type": "application/nostr+json"}), self.nip11_doc())
        # No NIP-05 server, no welcome page, no WoT probe: this answers one machine's own windows.
        return Response(200, "OK", Headers({"Content-Type": "text/plain; charset=utf-8"}),
                        b"PosterChan local relay\n")

    async def _dispatch(self, conn, raw) -> None:
        if isinstance(raw, str) and raw.startswith('["PC-SIGNED-AUTH"'):
            try:
                msg = json.loads(raw)
                self._signed_auth(conn, msg[1], msg[2] if len(msg) > 2 else None)
            except (ValueError, IndexError, TypeError):
                pass
            return
        await super()._dispatch(conn, raw)

    def _signed_auth(self, conn, rid, ev) -> None:
        w = self._sign_waiters.get(rid)
        if not w or w[0].done():
            return
        fut, owner, relay_url, challenge = w
        if owner not in self._auth_pubkeys.get(conn, set()):
            return
        ok = (isinstance(ev, dict) and ev.get("kind") == 22242 and ev.get("pubkey") == owner
              and self._tag_value(ev, "relay") == relay_url and self._tag_value(ev, "challenge") == challenge
              and verify_event(ev))
        fut.set_result(ev if ok else None)

    async def sign_upstream_auth(self, owner: str, relay_url: str, challenge: str, timeout: float = 20.0):
        """Ask the owner's client (authenticated on THIS socket) to sign the instance's NIP-42 challenge.
        None when no such client is connected or it declines -- the private documents then wait for the next
        pass (and still arrive through the client's own reads)."""
        conns = [c for c, pks in list(self._auth_pubkeys.items()) if owner in pks]
        if not conns or not challenge:
            return None
        rid = secrets.token_hex(8)
        fut = asyncio.get_running_loop().create_future()
        self._sign_waiters[rid] = (fut, owner, relay_url, challenge)
        try:
            for c in conns:
                self._send(c, ["PC-SIGN-AUTH", rid, relay_url, challenge])
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None
        finally:
            self._sign_waiters.pop(rid, None)


def server_config(cfg: dict) -> dict:
    """The RelayServer config of a loopback cache: open publishing (there is no web of trust here, and every
    write is a copy the user's client chose to keep), no moderation lists, no bridges, no DVMs, no VM hosting."""
    return {"bind": cfg["bind"], "port": cfg["port"], "wot_enabled": False, "block_json": False,
            "block_bridged": False, "posterchan_clients_only": False, "max_connections": 256,
            "max_subs_per_conn": 64, "max_filters_per_req": 20, "max_message_size": 8 * MB,
            "outq_size": 16384, "neg_max_items": NEG_MAX_ITEMS, "name": "PosterChan local relay"}


# ------------------------------------------------------------------------------------------------ the sync
class _Closed(Exception):
    pass


class _AuthRequired(_Closed):
    pass


class _NegUnsupported(Exception):
    pass


def scope(owner: str, follows: list, now: int) -> list:
    """[(name, filter)] -- everything this cache asks the instance for, and nothing else."""
    out = [("own", {"authors": [owner], "kinds": list(OWN_KINDS)}),
           ("private", {"authors": [owner], "kinds": [30078]}),
           ("mentions", {"#p": [owner], "kinds": list(MENTION_KINDS), "since": now - MENTION_WINDOW}),
           ("dms", {"#p": [owner], "kinds": [1059], "since": now - DM_WINDOW})]
    fl = [p for p in follows if isinstance(p, str) and len(p) == 64][:FOLLOWS_MAX]
    for i in range(0, len(fl), AUTHORS_PER_FILTER):
        out.append(("follows:%d" % (i // AUTHORS_PER_FILTER),
                    {"authors": fl[i:i + AUTHORS_PER_FILTER], "kinds": list(FOLLOW_KINDS),
                     "since": now - FOLLOW_WINDOW}))
    return out


class InstanceSync:
    def __init__(self, store: OsCacheStore, server: OsRelayServer, cfg: dict, *, connect=None, http_get=None):
        self.store, self.server, self.cfg = store, server, cfg
        self.connect = connect or _ws_connect
        self.http_get = http_get or _http_get_json
        self.interval = max(5, int(cfg.get("sync_interval") or 120))
        self._relay_of: dict = {}            # instance URL -> its relay URL (from /client/config)
        self._said: set = set()
        self.cursors: dict = {}
        self.passes = 0
        self.stored = 0
        self.last_error = ""
        self._reset()

    def _reset(self) -> None:
        self.ws = None
        self.url = ""
        self.challenge = ""
        self._subs: dict = {}
        self._oks: dict = {}

    def _say_once(self, key: str, msg: str, *a) -> None:
        if key not in self._said:
            self._said.add(key)
            logger.info(msg, *a)

    async def resolve(self) -> str:
        up = self.cfg.get("upstream") or ""
        if up:
            return up if _ws_url_ok(up) else ""
        inst, tor = chosen_instance(self.cfg["desktop_config"])
        if not inst:
            self._say_once("none", "[posterchan-relay] no instance chosen -- a local cache only")
            return ""
        if tor:
            self._say_once("tor", "[posterchan-relay] the desktop routes through Tor -- not syncing from %s "
                                  "directly (the client still fills this cache)", inst)
            return ""
        relay = self._relay_of.get(inst)
        if relay is None:
            conf = await asyncio.get_running_loop().run_in_executor(
                None, self.http_get, inst + "/client/config")
            relay = str((conf or {}).get("relay_url") or "")
            if not _ws_url_ok(relay):
                raise ValueError("%s/client/config named no relay" % inst)
            self._relay_of[inst] = relay
        return relay

    # ------------------------------------------------------------ the loop
    async def run(self, stop: asyncio.Event) -> None:
        backoff = 5
        while not stop.is_set():
            try:
                url = await self.resolve()
            except Exception as e:      # noqa: BLE001 -- unreachable instance = no upstream this round
                self.last_error = "resolve: %r" % (e,)
                url = ""
            if not url or not self.store.owner:
                # Waiting for the owner's client to sign in is a short poll (the first sync should follow the
                # first AUTH within seconds); waiting for an instance to be CHOSEN re-reads config.json often
                # enough that the welcome screen's choice is picked up without a restart.
                await _wait(stop, 2 if url else min(self.interval, 15))
                continue
            try:
                async with self.connect(url) as ws:
                    backoff = 5
                    await self._session(ws, url, stop)
            except asyncio.CancelledError:
                raise
            except Exception as e:      # noqa: BLE001 -- the instance went away; the cache keeps serving
                self.last_error = repr(e)
                logger.info("[posterchan-relay] instance %s: %r -- retrying in %ds", url, e, backoff)
                await _wait(stop, backoff)
                backoff = min(300, backoff * 2)
            finally:
                self._reset()

    async def _session(self, ws, url: str, stop: asyncio.Event) -> None:
        self.ws, self.url = ws, url
        reader = asyncio.create_task(self._reader(ws))
        try:
            while not stop.is_set() and not reader.done():
                owner = self.store.owner
                if owner:
                    await self._pass(owner)
                    self.passes += 1
                waiter = asyncio.create_task(stop.wait())
                await asyncio.wait({reader, waiter}, timeout=self.interval, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                try:
                    if await self.resolve() != url:
                        return                       # the welcome screen chose another instance
                except Exception:      # noqa: BLE001
                    return
        finally:
            reader.cancel()

    async def _reader(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, list) or not msg:
                continue
            typ = msg[0]
            if typ == "AUTH" and len(msg) >= 2:
                self.challenge = str(msg[1])
            elif typ == "OK" and len(msg) >= 3:
                fut = self._oks.pop(msg[1], None)
                if fut and not fut.done():
                    fut.set_result((bool(msg[2]), str(msg[3]) if len(msg) > 3 else ""))
            elif typ in ("EVENT", "EOSE", "CLOSED", "NEG-MSG", "NEG-ERR") and len(msg) >= 2:
                q = self._subs.get(msg[1])
                if q is not None:
                    q.put_nowait(msg)
            elif typ == "NOTICE":
                logger.debug("[posterchan-relay] instance NOTICE: %s", msg[1:2])

    async def _send(self, msg) -> None:
        await self.ws.send(json.dumps(msg))

    async def _req(self, filters: list, timeout: float = 30.0) -> list:
        sid = "pc" + secrets.token_hex(4)
        q: asyncio.Queue = asyncio.Queue()
        self._subs[sid] = q
        out = []
        try:
            await self._send(["REQ", sid] + filters)
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                msg = await asyncio.wait_for(q.get(), left)
                if msg[0] == "EVENT" and len(msg) >= 3:
                    out.append(msg[2])
                elif msg[0] == "EOSE":
                    break
                elif msg[0] == "CLOSED":
                    why = str(msg[2]) if len(msg) > 2 else ""
                    raise (_AuthRequired if why.startswith("auth-required") else _Closed)(why)
        finally:
            self._subs.pop(sid, None)
            try:
                await self._send(["CLOSE", sid])
            except Exception:      # noqa: BLE001
                pass
        return out

    async def _neg(self, flt: dict, items: list, timeout: float = 30.0) -> set:
        sid = "pn" + secrets.token_hex(4)
        q: asyncio.Queue = asyncio.Queue()
        self._subs[sid] = q
        ini = negentropy.Initiator(items)
        try:
            await self._send(["NEG-OPEN", sid, flt, ini.initiate().hex()])
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise _NegUnsupported("timeout")
                msg = await asyncio.wait_for(q.get(), left)
                if msg[0] == "NEG-ERR":
                    why = str(msg[2]) if len(msg) > 2 else ""
                    if why.startswith("auth-required"):
                        raise _AuthRequired(why)
                    raise _NegUnsupported(why)
                if msg[0] != "NEG-MSG" or len(msg) < 3:
                    continue
                nxt = ini.reconcile(bytes.fromhex(msg[2]))
                if nxt is None:
                    return {i.hex() for i in ini.need}
                await self._send(["NEG-MSG", sid, nxt.hex()])
        finally:
            self._subs.pop(sid, None)
            try:
                await self._send(["NEG-CLOSE", sid])
            except Exception:      # noqa: BLE001
                pass

    async def _auth(self, owner: str) -> bool:
        if not self.challenge:
            return False
        ev = await self.server.sign_upstream_auth(owner, self.url, self.challenge)
        if not ev:
            return False
        fut = asyncio.get_running_loop().create_future()
        self._oks[ev["id"]] = fut
        await self._send(["AUTH", ev])
        try:
            ok, _ = await asyncio.wait_for(fut, 15)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return ok

    async def _keep(self, flt: dict, events: list) -> int:
        """Store what the instance sent -- only signed events that MATCH what was asked for: an instance is
        trusted to answer, never to put arbitrary content into this machine's cache."""
        good = [ev for ev in events if isinstance(ev, dict) and _matches([flt], ev) and verify_event(ev)]
        if not good:
            return 0
        n = await self.store.add_events_bulk(good, origin="wot")
        self.stored += n
        return n

    async def _sync_filter(self, name: str, flt: dict) -> int:
        items = await self.store.neg_items([flt], cap=NEG_MAX_ITEMS)
        try:
            need = sorted(await self._neg(flt, items))
            got = 0
            for i in range(0, len(need), IDS_PER_REQ):
                ids = need[i:i + IDS_PER_REQ]
                got += await self._keep({"ids": ids}, [e for e in await self._req([{"ids": ids}])
                                                        if _matches([flt], e)])
        except _NegUnsupported:
            since = self.cursors.get(name)
            req = dict(flt, limit=500)
            if since:
                req["since"] = max(int(flt.get("since") or 0), since - 600)
            got = await self._keep(flt, await self._req([req]))
        self.cursors[name] = int(time.time())
        return got

    async def _follows(self, owner: str) -> list:
        own = await self.store.query([{"authors": [owner], "kinds": [3], "limit": 1}], 1)
        if not own:
            try:
                await self._keep({"authors": [owner], "kinds": [3]},
                                 await self._req([{"authors": [owner], "kinds": [3], "limit": 1}]))
            except _Closed:
                pass
            own = await self.store.query([{"authors": [owner], "kinds": [3], "limit": 1}], 1)
        if not own:
            return []
        return [t[1] for t in own[0].get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == "p"]

    async def _pass(self, owner: str) -> None:
        follows = await self._follows(owner)
        for name, flt in scope(owner, follows, int(time.time())):
            try:
                await self._sync_filter(name, flt)
            except _AuthRequired:
                if await self._auth(owner):
                    try:
                        await self._sync_filter(name, flt)
                    except _Closed as e:
                        logger.info("[posterchan-relay] %s refused after AUTH: %s", name, e)
                else:
                    self._say_once("noauth:" + name, "[posterchan-relay] %s needs the user's AUTH; waiting "
                                                     "for their client", name)
            except _Closed as e:
                logger.info("[posterchan-relay] %s refused: %s", name, e)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except (asyncio.TimeoutError, TimeoutError):
        pass


# ------------------------------------------------------------------------------------------------ the process
def _write_ready(path: str, url: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(url + "\n")
    os.replace(tmp, path)


async def run(cfg: dict, store: OsCacheStore, stop: asyncio.Event, *, connect=None, http_get=None) -> None:
    from websockets.asyncio.server import serve
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    server = OsRelayServer(store, WotGate(), server_config(cfg),
                           on_owner=store.set_owner)
    ws = await serve(server.handle, cfg["bind"], cfg["port"], process_request=server.process_request,
                     max_size=8 * MB, ping_interval=30, ping_timeout=120, max_queue=256)
    url = "ws://%s:%d/" % (cfg["bind"], cfg["port"])
    # Written only once the listener is bound: pc-shell-start-wayfire waits for this file before it starts the
    # desktop, and the desktop reads it to find this relay (desktop/main.js localRelayUrl).
    _write_ready(cfg["ready_file"], url)
    logger.info("[posterchan-relay] listening on %s (cache %s, cap %d MB)", url, cfg["data_dir"],
                cfg["max_bytes"] // MB)
    sync = InstanceSync(store, server, cfg, connect=connect, http_get=http_get)

    async def _housekeeping():
        last_save = time.monotonic()
        while not stop.is_set():
            await _wait(stop, cfg.get("prune_interval", 300))
            try:
                removed = await store.prune()
                if removed:
                    logger.info("[posterchan-relay] evicted %d least-recently-used events (cap %d MB)",
                                removed, cfg["max_bytes"] // MB)
            except Exception as e:      # noqa: BLE001
                logger.warning("[posterchan-relay] eviction failed: %r", e)
            if time.monotonic() - last_save >= 3600:
                last_save = time.monotonic()
                await asyncio.get_running_loop().run_in_executor(None, store.save_used)

    tasks = [asyncio.create_task(sync.run(stop)), asyncio.create_task(_housekeeping())]
    try:
        await stop.wait()
    finally:
        try:
            os.remove(cfg["ready_file"])
        except OSError:
            pass
        for t in tasks:
            t.cancel()
        ws.close()
        try:
            await asyncio.wait_for(ws.wait_closed(), 5)
        except Exception:      # noqa: BLE001
            pass
        try:
            await store.checkpoint()
        except Exception:      # noqa: BLE001
            pass


def prepare_dir(path: str) -> None:
    """The cache directory, NOCOW on btrfs (the same rule as every other PosterChanDB directory: btrfs
    fragments an append-only log). `chattr +C` only reaches files created after it, so recursively while the
    directory is empty, and on the directory itself once it holds data. The unit's launcher
    (os/bin/pc-local-relay) does the same before this runs; doing it here too covers a hand start."""
    import subprocess
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        fs = subprocess.run(["stat", "-f", "-c", "%T", path], capture_output=True, text=True, timeout=5).stdout
        if fs.strip() != "btrfs":
            return
        subprocess.run(["chattr", "-R", "+C", path] if not os.listdir(path) else ["chattr", "+C", path],
                       capture_output=True, timeout=30)
    except Exception:      # noqa: BLE001 -- NOCOW is an optimisation; a cache that runs is the point
        pass


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    cfg = read_config()
    prepare_dir(cfg["data_dir"])
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store = OsCacheStore(cfg["data_dir"], max_bytes=cfg["max_bytes"], flush_interval=cfg["flush_s"])
    store.open(loop)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        loop.run_until_complete(run(cfg, store, stop))
    finally:
        store.close()
        loop.close()
    return 0
