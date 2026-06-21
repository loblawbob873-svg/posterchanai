"""Settings read-path → Nostr relay (Phase 1 of the Nostr-as-datastore migration).

The app reads global settings from the SQLite `Setting` table in ~50 places, via many small
`db.query(Setting)` helpers. Refactoring every caller to read the relay synchronously would be
huge and slow (each read = an async WS round-trip + decrypt). Instead we make the relay the
**authoritative** store and keep the `Setting` table as a fast local **read-through cache**:

  * `hydrate(db)`  — at startup (relay → Setting): pull every `pcai:setting:` doc and UPSERT it
    into the `Setting` table, so all existing synchronous readers transparently see relay values.
  * `write_through(db, changes)` — on admin save (Setting → relay): mirror changed keys to the
    relay so it stays authoritative.

Flag-gated by the `settings_backend` setting (`relay` enables it; default `sqlite` = off, so
production/fresh nodes are unaffected until explicitly flipped). The relay's event store is a
*separate* SQLite file; "no app DB" means the app DB stops being the source of truth, with the
`Setting` table demoted to a derived cache.
"""

import logging
import os
import json
import threading

from app.services import nostr_store as store
from app.services import nostr_migrate as _mig

logger = logging.getLogger(__name__)

# Datastore-plumbing keys are always sourced from the local Setting cache, never hydrated/
# written-through — so a stale relay copy can't change where/whether we connect to the relay.
_PLUMBING_KEYS = frozenset({
    "nostr_relay_port", "nostr_relay_enabled", "nostr_relay_bind", "nostr_relay_pg_dsn",
})

# Per-node RUNTIME-STATE settings (sync cursors / seen-sets) are advanced directly in SQLite by the
# pollers, NOT via admin Save — so write-through never sees them and the relay copy goes stale. They
# are also inherently per-node (each node has its own sync position), so they must stay local: never
# hydrate (a stale relay cursor would reset progress → re-post old content) and never write-through.
_RUNTIME_KEYS = frozenset({"nitter_seen", "autopost_last_runs", "fedi_timeline_since"})
_RUNTIME_SUFFIXES = ("_since", "_seen", "_cursor", "_last_runs", "_next_batch")


def _is_local_only(key: str) -> bool:
    return key in _PLUMBING_KEYS or key in _RUNTIME_KEYS or key.endswith(_RUNTIME_SUFFIXES)


# ============================================================================================
# In-process settings store (NO SQL Setting table). The relay is authoritative for shareable
# settings (hydrated into _CACHE at startup); local-only keys (plumbing + per-node runtime cursors)
# persist in a small JSON file in the data dir. All ~app-wide settings reads/writes go through the
# get*/put/delete accessors below — there is no `Setting` ORM table anymore.
# ============================================================================================
_CACHE: dict = {}
_lock = threading.RLock()
_loaded = False

_LOCAL_PATH = os.environ.get(
    "POSTERCHANAI_LOCAL_SETTINGS",
    os.path.join(os.environ.get("POSTERCHANAI_DATA", "/var/lib/posterchanai"), "local_settings.json"),
)


def _load_local_file() -> dict:
    try:
        with open(_LOCAL_PATH) as f:
            d = json.load(f)
            return {str(k): ("" if v is None else str(v)) for k, v in d.items()} if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("[settings-store] could not read %s: %s", _LOCAL_PATH, e)
        return {}


def _save_local_file() -> None:
    """Persist ONLY the local-only keys (plumbing + runtime cursors) to the JSON file. Atomic."""
    local = {k: v for k, v in _CACHE.items() if _is_local_only(k)}
    tmp = _LOCAL_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_LOCAL_PATH), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(local, f)
        os.replace(tmp, _LOCAL_PATH)
    except Exception as e:
        logger.warning("[settings-store] could not write %s: %s", _LOCAL_PATH, e)


# ---- synchronous read accessors (replace every db.query(Setting) read) ----
def get(key: str, default=None):
    """The value for `key` (str) from the in-process cache, or `default` if unset."""
    with _lock:
        v = _CACHE.get(key)
    return default if v is None else v


def get_bool(key: str, default: bool = False) -> bool:
    v = get(key, None)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_int(key: str, default: int = 0) -> int:
    try:
        v = get(key, None)
        return default if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        v = get(key, None)
        return default if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return default


def all_settings() -> dict:
    with _lock:
        return dict(_CACHE)


def prefixed(prefix: str) -> dict:
    with _lock:
        return {k: v for k, v in _CACHE.items() if k.startswith(prefix)}


def exists(key: str) -> bool:
    with _lock:
        return key in _CACHE


# ---- writes ----
def _set_local(key: str, value) -> bool:
    """Update the in-process cache (+ persist local-only keys to the JSON file). Returns changed."""
    sval = "" if value is None else str(value)
    with _lock:
        if _CACHE.get(key) == sval:
            return False
        _CACHE[key] = sval
        local = _is_local_only(key)
    if local:
        _save_local_file()
    return True


def put(key: str, value, *, write_relay: bool = True) -> None:
    """Set a setting. Local-only keys persist to the JSON file; shareable keys also write through to
    the relay (best-effort, fire-and-forget from sync callers)."""
    changed = _set_local(key, value)
    if not changed or _is_local_only(key) or not write_relay:
        return
    _schedule_relay_write({key: "" if value is None else str(value)})


def put_many(changes: dict, *, write_relay: bool = True) -> None:
    relay = {}
    for k, v in (changes or {}).items():
        if _set_local(k, v) and not _is_local_only(k):
            relay[k] = "" if v is None else str(v)
    if write_relay and relay:
        _schedule_relay_write(relay)


def delete(key: str) -> None:
    with _lock:
        existed = key in _CACHE
        _CACHE.pop(key, None)
        local = _is_local_only(key)
    if local:
        _save_local_file()
    elif existed:
        _schedule_relay_delete(key)


def enabled(db) -> bool:
    """The relay is the ONLY datastore — always on (the legacy sqlite/table-authoritative mode is
    gone). The Postgres `settings` table is just a hydrated read-cache."""
    return True


def ensure_operator_key(db) -> bool:
    """Resolve (and on a fresh node, MINT) the datastore operator/signer key into the keyfile.
    Must run BEFORE the relay starts so the relay's operator set includes this pubkey — otherwise
    the relay rejects its own first settings doc as "not in web of trust" (the operator key was
    minted lazily, after the relay had already read an empty operator set). Returns True if a key
    is available. Idempotent."""
    try:
        return _operator_seckey(db) is not None
    except Exception as e:
        logger.warning("[settings-store] ensure_operator_key failed: %s", e)
        return False


def _operator_seckey(db):
    """The operator's secret key — needed to read/write the operator-signed docs. Sourced from the
    local keyfile (authoritative); falls back to the admin's `User.nostr_nsec` and migrates it into
    the keyfile on first use (so it survives the app DB being eliminated). None if not configured."""
    from app.services import keystore
    from app.services.nostr import nostr_service
    nsec = keystore.get_operator_nsec()
    if not nsec:
        from app.models import User
        op = db.query(User).filter(User.is_admin == True, User.nostr_nsec.isnot(None)).first()  # noqa: E712
        if op and op.nostr_nsec:
            nsec = op.nostr_nsec
            keystore.set_operator_nsec(nsec)   # migrate into the keyfile
    if not nsec:
        # No operator key anywhere → mint one and persist it to the keyfile, so a fresh node is a
        # self-sufficient relay datastore (can sign its own settings/users/bots docs). This is what
        # makes "relay backend by default" work out of the box on a brand-new install.
        import secrets
        from app.services.nostr import bech32, bip340
        for _ in range(8):
            cand = secrets.token_bytes(32)
            try:
                bip340.pubkey_from_seckey(cand)
            except Exception:
                continue
            nsec = bech32.encode("nsec", cand)
            keystore.set_operator_nsec(nsec)
            logger.info("[settings-store] minted a fresh operator key (nostr datastore signer)")
            break
    if not nsec:
        return None
    try:
        return nostr_service.decode_seckey(nsec)
    except Exception:
        return None


def _port() -> int:
    return get_int("nostr_relay_port", 3052)


# ---- startup population of the in-process cache ----
def load_local() -> None:
    """Load the local-only keys (plumbing + cursors) from the JSON file into the cache. Call EARLY
    (before the relay starts) so nostr_relay_port/bind/enabled are available to reach the relay."""
    with _lock:
        _CACHE.update(_load_local_file())


def apply_defaults(defaults: dict) -> None:
    """Seed default values into the cache for keys not already present (so reads work out of the box
    even before the relay hydrate). Relay values override these in hydrate(). Local-only defaults are
    also persisted to the JSON file."""
    wrote_local = False
    with _lock:
        for k, v in (defaults or {}).items():
            if k not in _CACHE:
                _CACHE[k] = "" if v is None else str(v)
                if _is_local_only(k):
                    wrote_local = True
    if wrote_local:
        _save_local_file()


async def hydrate(db) -> int:
    """relay → cache. Pull every setting doc from the relay into the in-process cache (authoritative
    for shareable keys). Caches the operator key for the background relay-writer. No-op without an
    operator key. Returns the number of keys updated."""
    global _OP_SK
    op_sk = _operator_seckey(db)
    if not op_sk:
        logger.info("[settings-store] hydrate skipped — no operator key")
        return 0
    _OP_SK = op_sk
    try:
        relay = await _mig.settings_all(_port(), op_sk)
    except Exception as e:
        logger.warning("[settings-store] hydrate failed to read relay: %s", e)
        return 0
    changed = 0
    for key, value in (relay or {}).items():
        if _is_local_only(key):   # a stale relay copy must not change plumbing or reset a cursor
            continue
        if _set_local(key, value):
            changed += 1
    logger.info("[settings-store] hydrated %d setting(s) from relay", changed)
    return changed


async def write_through(db, changes: dict) -> int:
    """Mirror settings → relay (authoritative). Used by the background writer and admin save."""
    if not changes:
        return 0
    op_sk = _OP_SK or _operator_seckey(db)
    if not op_sk:
        return 0
    port = _port()
    wrote = 0
    for key, value in changes.items():
        if _is_local_only(key):
            continue
        try:
            if await store.put_doc(port, op_sk, store.NS_SETTING + key, {"value": value}):
                wrote += 1
        except Exception as e:
            logger.warning("[settings-store] write-through failed for %s: %s", key, e)
    if wrote:
        logger.info("[settings-store] wrote %d setting(s) through to relay", wrote)
    return wrote


async def seed_relay_defaults(db, defaults: dict) -> int:
    """First-boot seeding (relay ← defaults). Push every default whose key the relay does NOT yet
    hold UP to the relay, so it carries the out-of-box config from first start. Run AFTER hydrate()
    so keys the relay already has are skipped (never overwritten). Writes nothing on an established
    node. `defaults` is the canonical default_settings dict (NOT a table)."""
    op_sk = _OP_SK or _operator_seckey(db)
    if not op_sk:
        return 0
    try:
        relay = await _mig.settings_all(_port(), op_sk)
    except Exception as e:
        logger.warning("[settings-store] seed: failed to read relay: %s", e)
        return 0
    have = set((relay or {}).keys())
    missing = {k: ("" if v is None else str(v)) for k, v in (defaults or {}).items()
               if k not in have and not _is_local_only(k)}
    if not missing:
        return 0
    # also reflect into the cache so reads are correct immediately
    for k, v in missing.items():
        _set_local(k, v)
    wrote = await write_through(db, missing)
    if wrote:
        logger.info("[settings-store] seeded %d default setting(s) to the relay (first boot)", wrote)
    return wrote


# ---- background relay writer: bridges synchronous put()/delete() to async relay writes ----
_OP_SK = None
_writer_q = None
_writer_started = False


def _ensure_writer() -> None:
    global _writer_q, _writer_started
    if _writer_started:
        return
    import queue as _queue
    import asyncio as _asyncio
    _writer_q = _queue.Queue()

    def _run():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        while True:
            op = _writer_q.get()
            if not op:
                continue
            kind, payload = op
            try:
                if kind == "put":
                    loop.run_until_complete(write_through(None, payload))
                elif kind == "delete" and (_OP_SK):
                    loop.run_until_complete(store.delete_doc(_port(), _OP_SK, store.NS_SETTING + payload))
            except Exception as e:
                logger.warning("[settings-store] relay writer error: %s", e)

    threading.Thread(target=_run, daemon=True, name="settings-relay-writer").start()
    _writer_started = True


def _schedule_relay_write(changes: dict) -> None:
    _ensure_writer()
    _writer_q.put(("put", dict(changes)))


def _schedule_relay_delete(key: str) -> None:
    _ensure_writer()
    _writer_q.put(("delete", key))
