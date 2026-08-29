"""Global settings store — Nostr relay is the ONLY datastore (no SQL `Setting` table).

Settings live as operator-signed `pcai:setting:` events in the relay's Postgres event store. The
app keeps an **in-process cache** (`_CACHE`) hydrated from the relay at startup, so the many
synchronous readers (`settings_store.get(...)`) are fast — no SQL `Setting` table, no per-read WS
round-trip. Writes go to the cache and are mirrored to the relay (`put`/`put_many`, via a
background relay-writer). Per-node LOCAL-ONLY keys (plumbing needed to reach the relay + runtime
cursors) can't live in the relay, so they persist in a small `local_settings.json` in the data dir.

  * `load_local()`            — load local-only keys from the JSON file (call before the relay starts)
  * `apply_defaults(d)`       — seed default values into the cache for keys not already present
  * `hydrate(db)`             — relay → cache (authoritative for shareable keys)
  * `seed_relay_defaults(db, d)` — first boot: push defaults the relay lacks UP to the relay
  * `get/get_bool/get_int/get_float/all_settings/prefixed/exists` — sync reads
  * `put/put_many/delete`    — writes (local JSON for local-only, relay for shareable)
"""

import logging
import os
import fcntl
import json
import threading

from app.services import nostr_store as store
from app.services import nostr_migrate as _mig

logger = logging.getLogger(__name__)

# Datastore-plumbing keys are always sourced from the local Setting cache, never hydrated/
# written-through — so a stale relay copy can't change where/whether we connect to the relay.
#
# The git-host TOPOLOGY keys (enabled/bind/port/proxy_url) are per-node for the SAME reason the
# relay's (nostr_relay_enabled/bind/port) are: WHETHER this node runs a git host, which interface/port
# it binds, and whether it instead PROXIES smart-HTTP to a peer that hosts (`git_server_proxy_url`),
# are per-node topology facts — e.g. nas.lan hosts on 0.0.0.0:3053 (enabled=true) while server1 sets
# proxy_url=http://nas.lan:3053, stays enabled=false, and runs no local host. If these hydrated from
# the shared relay doc, one node's enable/proxy_url would leak onto every node. `git_server_public_base`
# is per-node too: it's the PUBLIC url prefix external clients use to reach the repos THIS node hosts
# (e.g. nas announces https://poster.place/git — reached via the server1 edge → proxy → nas), and a repo
# hosted on a differently-fronted node would advertise a different base. The remaining POLICY keys
# (allowlist/repo_max_mb/default_private/…) stay shareable/global — same repos + rules on every node.
# Mirrors nostr_relay_enabled being plumbing.
_PLUMBING_KEYS = frozenset({
    "nostr_relay_port", "nostr_relay_enabled", "nostr_relay_bind", "nostr_relay_pg_dsn",
    "git_server_enabled", "git_server_bind", "git_server_port", "git_server_proxy_url",
    "git_server_public_base",
})

# Per-node RUNTIME-STATE settings (sync cursors / seen-sets) are advanced directly in SQLite by the
# pollers, NOT via admin Save — so write-through never sees them and the relay copy goes stale. They
# are also inherently per-node (each node has its own sync position), so they must stay local: never
# hydrate (a stale relay cursor would reset progress → re-post old content) and never write-through.
_RUNTIME_KEYS = frozenset({"autopost_last_runs", "autopost_daily_counts",
                           "fedi_timeline_since", "stats_counters", "stats_counters_hourly"})
_RUNTIME_SUFFIXES = ("_since", "_seen", "_cursor", "_last_runs", "_next_batch")


def _is_local_only(key: str) -> bool:
    # NOTE: turn_* (calls TURN config, incl. turn_shared_secret) are NOT local — they persist on the relay
    # and hydrate like every other admin setting. That's safe because the operator's settings doc is stored
    # NIP-44-ENCRYPTED on the relay (only the operator key can read it), so the secret never leaks in clear.
    # A node without the pion-turn binary just no-ops the relay, so multi-node stays correct.
    return key in _PLUMBING_KEYS or key in _RUNTIME_KEYS or key.endswith(_RUNTIME_SUFFIXES)


# ============================================================================================
# In-process settings store (NO SQL Setting table). The relay is authoritative for shareable
# settings (hydrated into _CACHE at startup); local-only keys (plumbing + per-node runtime cursors)
# persist in a small JSON file in the data dir. All ~app-wide settings reads/writes go through the
# get*/put/delete accessors below — there is no `Setting` ORM table anymore.
# ============================================================================================
_CACHE: dict = {}
# local-only keys THIS process has written; only these are flushed to the shared JSON file.
_LOCAL_DIRTY: set = set()
_lock = threading.RLock()
_loaded = False
_HYDRATED_KEYS: set = set()   # keys for which the relay holds an authoritative value (vs a default)
_LOCAL_KEYS: set = set()      # keys loaded from the local JSON (vs a default)
# True once this process has successfully synced the cache with the relay at least once. Guards
# read-modify-write of replaceable LIST settings (e.g. blossom_whitelist, nostr_relay_nip05_names):
# before this is set, get(key) may return "" merely because the cache isn't loaded — writing a merged
# list from that empty read would WIPE the real list. See project_blossom_whitelist_wipe.
_HYDRATED = False


def is_hydrated() -> bool:
    """Has the cache synced with the relay at least once? If False, an empty get() is 'not loaded yet',
    not 'genuinely empty' — so callers must NOT rewrite a list setting from it."""
    return _HYDRATED

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
    """Persist local-only keys to the JSON file — merging with what's on disk, under a lock.

    This file is shared by SEVERAL processes: the app (bot manager -> autopost_last_runs /
    autopost_daily_counts) and the worker (fedi bridge -> *_since / *_cursor). Dumping this
    process's whole cache used to clobber the other's keys: the worker writes a bridge cursor every
    poll, rewriting autopost_last_runs from the value IT read at startup, which silently reverted the
    manager's post times. A stale anchor makes `last_run + gap` permanently overdue, so every restart
    fired an immediate auto-post and the configured schedule never held.

    Only keys THIS process actually wrote (_LOCAL_DIRTY) are overlaid onto the on-disk state, so
    concurrent writers no longer overwrite each other's state. flock serialises the read-modify-write
    so two writers can't interleave."""
    tmp = _LOCAL_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_LOCAL_PATH), exist_ok=True)
        lock_path = _LOCAL_PATH + ".lock"
        with open(lock_path, "w") as lock_f:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass                                   # no flock (exotic fs) — still better than a clobber
            merged = _load_local_file()                # whatever other processes have written
            with _lock:
                for k in list(_LOCAL_DIRTY):
                    v = _CACHE.get(k)
                    if v is not None and _is_local_only(k):
                        merged[k] = v
            with open(tmp, "w") as f:
                json.dump(merged, f)
            os.replace(tmp, _LOCAL_PATH)
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    except Exception as e:
        logger.warning("[settings-store] could not write %s: %s", _LOCAL_PATH, e)


def bump_counter(key: str, day: str, metric: str, n: int = 1) -> None:
    """Increment data[day][metric] in a local-only JSON counter, at the FILE level under flock.

    Counters are incremented from whichever process does the work — image/music/video generation runs
    in the app, call signaling in the worker — so an in-memory dict is per-process and a periodic
    flush from the OTHER process writes nothing. (That is exactly why Server Stats showed 0 images and
    0 music after a day of generating: the app counted them, the worker flushed its own empty copy,
    and a restart discarded the app's.) Read-modify-write against the file makes the count correct no
    matter which process observed the event, and durable across the restarts this node does often.
    """
    try:
        os.makedirs(os.path.dirname(_LOCAL_PATH), exist_ok=True)
        with open(_LOCAL_PATH + ".lock", "w") as lock_f:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            disk = _load_local_file()
            try:
                data = json.loads(disk.get(key) or "{}")
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, TypeError):
                data = {}
            bucket = data.get(day)
            if not isinstance(bucket, dict):
                bucket = {}
            bucket[metric] = int(bucket.get(metric, 0)) + int(n)
            data[day] = bucket
            data = dict(sorted(data.items())[-90:])       # bounded: ~90 days is more than any chart
            disk[key] = json.dumps(data, separators=(",", ":"))
            tmp = _LOCAL_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(disk, f)
            os.replace(tmp, _LOCAL_PATH)
            with _lock:                                   # keep this process's cache in step
                _CACHE[key] = disk[key]
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    except Exception as e:
        logger.warning("[settings-store] counter bump failed (%s/%s): %s", key, metric, e)


def read_counter(key: str) -> dict:
    """The counter dict straight FROM DISK (another process may have written since our last read)."""
    try:
        data = json.loads(_load_local_file().get(key) or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
            _LOCAL_DIRTY.add(key)   # only keys we wrote get flushed — see _save_local_file
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
    if not nsec and db is not None:   # db may be None (e.g. the background relay-writer thread)
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


def ensure_admin(db) -> str | None:
    """Turnkey: on a fresh node with NO admin yet, make the auto-minted OPERATOR key the admin so AI
    works immediately — no manual web 'claim admin' click (which otherwise blocks testing the AI
    parts on a fresh install). Creates an admin User with the operator's npub + full grants and seeds
    the WoT with it. Idempotent + no-op once any admin with an npub exists; gated by
    POSTERCHANAI_AUTO_ADMIN (default on — set 0 to require a human to claim admin with their own key).
    Returns the admin npub if it provisioned/already-operator-admin, else None."""
    import os
    if os.environ.get("POSTERCHANAI_AUTO_ADMIN", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        from app.models import User
        from app.services.nostr import nostr_service
        import secrets, binascii
        # An admin with an npub already exists → instance is set up; don't override it.
        if db.query(User).filter(User.is_admin == True, User.nostr_npub.isnot(None)).first():  # noqa: E712
            return None
        op_sk = _operator_seckey(db)
        if not op_sk:
            return None
        pk = nostr_service.derive_pubkey(op_sk)
        pk_hex = pk if isinstance(pk, str) else binascii.hexlify(pk).decode()
        npub = nostr_service.npub_of(pk_hex)
        u = db.query(User).filter(User.nostr_npub == npub).first()
        if not u:
            from app.auth import get_password_hash
            base = "npub_" + npub[4:16]
            username = base
            for i in range(2, 100):
                if not db.query(User).filter(User.username == username).first():
                    break
                username = f"{base}{i}"
            u = User(username=username, email=None,
                     password_hash=get_password_hash(secrets.token_urlsafe(32)),
                     email_verified=True, nostr_npub=npub)
            db.add(u)
        u.is_admin = True
        u.can_ai = True
        u.can_image = True
        u.can_blossom = True
        db.commit()
        # Seed the WoT with the admin's own npub via the relay-authoritative settings store.
        val = get("nostr_relay_wot_seeds", "") or ""
        if npub not in val:
            put("nostr_relay_wot_seeds", (val.rstrip() + "\n" + npub).strip() if val.strip() else npub)
        logger.info("[settings-store] auto-provisioned admin from the operator key: %s", npub[:16])
        return npub
    except Exception as e:
        logger.warning("[settings-store] ensure_admin failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _port(db=None) -> int:
    # `db` is accepted (and ignored) so callers in users_store/bots_store/record_store can pass the
    # request session uniformly — the port is a local-only setting read from the in-process cache.
    return get_int("nostr_relay_port", 3052)


# ---- startup population of the in-process cache ----
def load_local() -> None:
    """Load the local-only keys (plumbing + cursors) from the JSON file into the cache. Call EARLY
    (before the relay starts) so nostr_relay_port/bind/enabled are available to reach the relay."""
    loaded = _load_local_file()
    with _lock:
        _CACHE.update(loaded)
        _LOCAL_KEYS.update(loaded.keys())   # remember which keys came from the JSON (vs a default)


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


def hydrate_from_db(db) -> int:
    """relay events → cache, SYNCHRONOUSLY, by reading the relay's `events` table directly (it's in
    the same Postgres) + NIP-44-decrypting each `pcai:setting:` doc. No WebSocket — so this works
    before the relay's WS is up AND inside the relay subprocess itself (which reads its own config).
    Authoritative for shareable keys; local-only keys are left to the JSON file. Caches the operator
    key for the background relay-writer. Returns the number of keys updated."""
    global _OP_SK, _HYDRATED
    op_sk = _operator_seckey(db)
    if not op_sk:
        return 0
    _OP_SK = op_sk
    try:
        from app.services.nostr import nostr_service, nip44
        import binascii, json as _json
        from sqlalchemy import text as _text
        pk = nostr_service.derive_pubkey(op_sk)
        op_hex = pk if isinstance(pk, str) else binascii.hexlify(pk).decode()
        rows = db.execute(_text(
            "SELECT DISTINCT ON (t.value) t.value AS d, e.content "
            "FROM events e JOIN event_tags t ON t.event_id = e.id "
            "WHERE e.kind = 30078 AND e.pubkey = :pk AND t.tag = 'd' AND t.value LIKE 'pcai:setting:%' "
            "ORDER BY t.value, e.created_at DESC"
        ), {"pk": op_hex}).fetchall()
    except Exception as e:
        # Roll back so the session leaves no aborted transaction for the caller's next query
        # (e.g. migrate_legacy_table reuses this same session right after).
        try:
            db.rollback()
        except Exception:
            pass
        # FRESH NODE: the relay hasn't created its `events`/`event_tags` tables yet (it owns that
        # schema and does it on first start), so the EARLY hydrate runs before they exist. Harmless —
        # there are no settings to read yet; defaults apply and the deferred hydrate (after the relay
        # is up) picks them up. Quietly note it instead of a scary warning.
        if "does not exist" in str(e) or "UndefinedTable" in type(e).__name__:
            logger.info("[settings-store] relay event store not initialized yet — using defaults for now")
            return 0
        logger.warning("[settings-store] hydrate_from_db failed: %s", e)
        return 0
    changed = 0
    for d, content in rows:
        key = d[len(store.NS_SETTING):]
        if _is_local_only(key):
            continue
        try:
            data = _json.loads(nip44.decrypt_self(op_sk, content))
            val = data.get("value") if isinstance(data, dict) else data
            _HYDRATED_KEYS.add(key)   # the relay holds an authoritative value for this key
            if _set_local(key, "" if val is None else str(val)):
                changed += 1
        except Exception:
            continue
    _HYDRATED = True   # relay event store read successfully (even 0 rows) → cache reflects the relay
    logger.info("[settings-store] hydrated %d setting(s) from relay events (sync)", changed)
    return changed


def migrate_legacy_table(db) -> int:
    """ONE-TIME data-safety: if a pre-no-db node still has the old SQL `settings` table, copy any key
    the relay does NOT already hold into the relay (via put → relay event), so the node's CUSTOM
    values survive the table going away. Keys the relay already has (hydrated above) and local-only
    keys are skipped. Idempotent; harmless when the table is gone. Returns rows migrated."""
    from sqlalchemy import text as _text, inspect as _inspect
    try:
        if not _inspect(db.bind).has_table("settings"):
            return 0
        rows = db.execute(_text("SELECT key, value FROM settings")).fetchall()
    except Exception as e:
        logger.warning("[settings-store] legacy table read failed: %s", e)
        return 0
    migrated = 0
    for key, value in rows:
        # Skip keys an authoritative source already provided: the relay (shareable) or the local JSON
        # (plumbing/cursors). Everything else — including custom local-only values like a non-default
        # nostr_relay_bind — is migrated (put routes it: shareable→relay, local-only→JSON).
        if key in _HYDRATED_KEYS or key in _LOCAL_KEYS:
            continue
        put(key, value if value is not None else "")   # legacy value beats the code default
        (_HYDRATED_KEYS if not _is_local_only(key) else _LOCAL_KEYS).add(key)
        migrated += 1
    if migrated:
        logger.info("[settings-store] migrated %d legacy settings-table key(s) into the relay", migrated)
    return migrated


async def hydrate(db) -> int:
    """relay → cache. Pull every setting doc from the relay into the in-process cache (authoritative
    for shareable keys). Caches the operator key for the background relay-writer. No-op without an
    operator key. Returns the number of keys updated."""
    global _OP_SK, _HYDRATED
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
    _HYDRATED = True   # relay read succeeded → cache reflects the relay
    logger.info("[settings-store] hydrated %d setting(s) from relay", changed)
    return changed


# pcai: CONFIG d-tags eligible for DR backup/restore (mirror server.py:_BACKUP_NS).
_BACKUP_NS = ("pcai:setting:", "pcai:user:", "pcai:usercfg:", "pcai:bot:")


async def restore_from_upstream(db) -> int:
    """Disaster recovery: pull the operator's encrypted pcai: CONFIG docs (settings/accounts/per-user
    config/bots) from the UPSTREAM relays and re-store them in the LOCAL relay, so a node whose
    settings were wiped (or a fresh node whose Postgres is empty) gets its config back (the docs are
    NIP-44 ciphertext to everyone but us — the operator nsec must be supplied out-of-band). Returns
    the number of docs restored. Caller should re-hydrate afterwards.

    CRITICAL (the bug that made restore a silent no-op): kind-30078 is parameterized-replaceable, so
    the relay keeps only the NEWEST created_at per d-tag and REJECTS an incoming older one. A wipe
    re-seeds defaults with a FRESH timestamp, so the upstream backup docs (older) would be rejected —
    publishing them verbatim restores nothing. So we RE-STAMP: re-sign each doc with the operator key
    and created_at=now (keeping its ciphertext content + tags), making it strictly newer than the
    wiped default so the relay adopts it. We restore the NEWEST upstream version per d-tag only."""
    op_sk = _OP_SK or _operator_seckey(db)
    if not op_sk:
        logger.warning("[settings-store] restore: no operator key")
        return 0
    from app.services.nostr import nostr_service, relay as _relay
    from app.services.nostr.event import build_event
    try:
        pk = nostr_service.derive_pubkey(op_sk)
        op_hex = pk if isinstance(pk, str) else pk.hex()
    except Exception as e:
        logger.warning("[settings-store] restore: cannot derive operator pubkey: %s", e)
        return 0
    upstream = _relay.normalize_relays(get("nostr_relay_upstream_relays", "")) or list(nostr_service.DEFAULT_RELAYS)
    try:
        evs = await _relay.query(upstream, [{"authors": [op_hex], "kinds": [store.APP_KIND]}], timeout=25)
    except Exception as e:
        logger.warning("[settings-store] restore: upstream query failed: %s", e)
        return 0
    # Keep only CONFIG docs, newest upstream version per d-tag (a relay may return stale duplicates).
    newest: dict = {}
    for ev in evs or []:
        d = next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "d"), "")
        if not d.startswith(_BACKUP_NS):
            continue   # only the small CONFIG set — never bulk chat/upload docs
        if d not in newest or ev.get("created_at", 0) > newest[d].get("created_at", 0):
            newest[d] = ev
    if not newest:
        logger.warning("[settings-store] restore: no CONFIG docs found on %d upstream relay(s)", len(upstream))
        return 0
    # One timestamp for the whole batch so the restore is a single coherent generation. +1s guards the
    # edge where a wipe re-seeded defaults in this very second (the relay replaces on created_at <=).
    import time as _time
    stamp = int(_time.time()) + 1
    local_url = f"ws://127.0.0.1:{_port()}"
    restored = 0
    for d, ev in newest.items():
        try:
            # Re-sign with the operator key + a fresh created_at so it beats the wiped default. The
            # content is the operator's own NIP-44 ciphertext — re-signing keeps it decryptable by us.
            restamped = build_event(op_sk, int(ev.get("kind", store.APP_KIND)),
                                    ev.get("content", ""), tags=ev.get("tags", []), created_at=stamp)
            if await _relay.publish([local_url], restamped, direct=True):
                restored += 1
        except Exception as e:
            logger.warning("[settings-store] restore: re-store %s failed: %s", d, e)
            continue
    logger.info("[settings-store] restored %d datastore doc(s) (re-stamped) from %d upstream relay(s) (DR)",
                restored, len(upstream))
    return restored


async def republish_datastore(db, *, pace: float = 0.3) -> dict:
    """DR BACKUP (the counterpart of restore_from_upstream): re-publish ALL of the operator's encrypted
    CONFIG docs (settings/users/usercfg/bots) so the relay outbox federates the FULL current config to
    upstream. Needed because federation is incremental — only docs WRITTEN since backup_datastore was
    enabled reach upstream, so the bulk of existing settings would otherwise never be backed up.

    Each doc is re-put with the operator key (fresh event → broadcastable → outbox), and the publishing
    is PACED so the bounded outbox queue can't overflow (which would silently drop docs from the fan-out).
    Returns {namespace: count}. Only actually federates when nostr_relay_backup_datastore is on."""
    import asyncio
    from app.services import nostr_store
    op_sk = _OP_SK or _operator_seckey(db)
    if not op_sk:
        return {"error": "no operator key"}
    port = _port()
    counts: dict = {}
    for ns in _BACKUP_NS:
        try:
            docs = await nostr_store.list_docs(port, ns, seckey=op_sk, encrypt=True)
        except Exception as e:
            logger.warning("[settings-store] backup: list %s failed: %s", ns, e)
            continue
        n = 0
        for d_tag, content in docs.items():
            if content is None:
                continue
            try:
                if await nostr_store.put_doc(port, op_sk, d_tag, content, encrypt=True):
                    n += 1
            except Exception as e:
                logger.warning("[settings-store] backup: republish %s failed: %s", d_tag, e)
            await asyncio.sleep(pace)   # keep the producer at/under the outbox drain rate
        counts[ns.rstrip(":").split(":")[-1]] = n
    logger.info("[settings-store] DR backup: republished config docs %s (federating via outbox)", counts)
    return counts


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


def _relay_setting_keys_from_db(db):
    """Race-free: the operator's existing `pcai:setting:` keys read DIRECTLY from the relay's Postgres
    `events` table (same source as hydrate_from_db). Returns (keys:set, authoritative:bool).

    Why not _mig.settings_all(): that queries the relay over the WebSocket, which under startup load
    can return a PARTIAL result — and seed_relay_defaults treating a falsely-"missing" key as absent is
    how real settings got overwritten by defaults (the 2026-06-23 wipe: "seeded 119 default setting(s)"
    while the events table actually held 288). A direct SQL read can't be raced by relay sync/load.
    authoritative=False means the read could not be trusted → the caller MUST NOT seed."""
    op_sk = _OP_SK or _operator_seckey(db)
    if not op_sk:
        return set(), False
    try:
        from app.services.nostr import nostr_service
        from sqlalchemy import text as _text
        import binascii
        pk = nostr_service.derive_pubkey(op_sk)
        op_hex = pk if isinstance(pk, str) else binascii.hexlify(pk).decode()
        rows = db.execute(_text(
            "SELECT DISTINCT t.value AS d FROM events e JOIN event_tags t ON t.event_id = e.id "
            "WHERE e.kind = 30078 AND e.pubkey = :pk AND t.tag = 'd' AND t.value LIKE 'pcai:setting:%'"
        ), {"pk": op_hex}).fetchall()
        pfx = store.NS_SETTING
        return {r[0][len(pfx):] for r in rows if r[0].startswith(pfx)}, True
    except Exception as e:
        try:
            db.rollback()   # leave no aborted txn for the caller's next query
        except Exception:
            pass
        # The relay hasn't created its event tables yet → GENUINE fresh node: empty + trustworthy,
        # so first-boot seeding is correct. Any other error is NOT trustworthy → refuse to seed.
        if "does not exist" in str(e) or "UndefinedTable" in type(e).__name__:
            return set(), True
        logger.warning("[settings-store] seed: direct key read failed: %s", e)
        return set(), False


async def seed_relay_defaults(db, defaults: dict) -> int:
    """First-boot seeding (relay ← defaults). Push every default whose key the relay does NOT yet
    hold UP to the relay, so it carries the out-of-box config from first start. Run AFTER hydrate()
    so keys the relay already has are skipped (never overwritten). Writes nothing on an established
    node. `defaults` is the canonical default_settings dict (NOT a table).

    FAIL-SAFE: determines what the relay already holds via a RACE-FREE direct Postgres read, and if
    that read is not authoritative it seeds NOTHING — overwriting the durable Nostr store with defaults
    because a transient read came back short is exactly the data-loss this whole design must prevent."""
    op_sk = _OP_SK or _operator_seckey(db)
    if not op_sk:
        return 0
    have, authoritative = _relay_setting_keys_from_db(db)
    if not authoritative:
        logger.warning("[settings-store] seed: relay state not authoritatively readable — skipping seed "
                       "to protect the durable store (defaults still apply in-memory this boot)")
        return 0
    missing = {k: ("" if v is None else str(v)) for k, v in (defaults or {}).items()
               if k not in have and not _is_local_only(k)}
    if not missing:
        return 0
    # also reflect into the cache so reads are correct immediately
    for k, v in missing.items():
        _set_local(k, v)
    wrote = await write_through(db, missing)
    if wrote:
        first = "first boot" if not have else "new keys on upgrade"
        logger.info("[settings-store] seeded %d default setting(s) to the relay (%s)", wrote, first)
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
        import time as _time
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        while True:
            op = _writer_q.get()
            if not op:
                continue
            kind, payload = op[0], op[1]
            attempt = op[2] if len(op) > 2 else 0
            try:
                if kind == "put":
                    # The relay (datastore) may be momentarily unreachable — during its startup before
                    # it binds 3052, or the few seconds it's restarting to apply a topology change.
                    # A write dropped then is LOST (settings live only in the relay), which is the
                    # "my settings never save" bug. So if not every key persisted, RE-QUEUE with backoff
                    # until the relay is back (write_through is idempotent). Bounded so we can't spin.
                    expected = sum(1 for k in payload if not _is_local_only(k))
                    wrote = loop.run_until_complete(write_through(None, payload))
                    if wrote < expected and attempt < 40:
                        _time.sleep(min(3.0, 0.4 * (attempt + 1)))
                        _writer_q.put(("put", payload, attempt + 1))
                    elif wrote < expected:
                        logger.error("[settings-store] gave up persisting %s after %d retries (relay unreachable)",
                                     list(payload.keys()), attempt)
                elif kind == "delete" and (_OP_SK):
                    loop.run_until_complete(store.delete_doc(_port(), _OP_SK, store.NS_SETTING + payload))
            except Exception as e:
                logger.warning("[settings-store] relay writer error: %s", e)
                if kind == "put" and attempt < 40:
                    _time.sleep(min(3.0, 0.4 * (attempt + 1)))
                    _writer_q.put(("put", payload, attempt + 1))

    threading.Thread(target=_run, daemon=True, name="settings-relay-writer").start()
    _writer_started = True


def _schedule_relay_write(changes: dict) -> None:
    _ensure_writer()
    _writer_q.put(("put", dict(changes)))


def _schedule_relay_delete(key: str) -> None:
    _ensure_writer()
    _writer_q.put(("delete", key))
