"""Local key store — the ONE piece of durable local state that is NOT the relay.

The per-user `storage_nsec` and the operator `nsec` *encrypt* the relay's app-data docs, so they can't
live inside the relay (circular: a cold start would have nothing to decrypt with). They live here, in
a small gitignored JSON keyfile (`data/keys.json`), keyed by **npub** (stable across in-memory app-DB
rebuilds, unlike the SQLite row id). This is what lets `posterchanai.db` be eliminated: all *data*
moves to the relay, only the key material stays local.

Shape:
    {"operator_nsec": "<nsec or hex>", "storage": {"<npub>": "<hex seckey>"}}

Reads prefer the keyfile and fall back to the legacy app.db locations (UserSetting `storage_nsec` /
admin `User.nostr_nsec`), migrating them in on first touch — so the cutover is seamless and reversible.
Writes are atomic (temp + os.replace) under a process lock.
"""

import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager

import fcntl

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_KEYFILE = os.environ.get("POSTERCHANAI_KEYFILE", os.path.join(_REPO_ROOT, "data", "keys.json"))
_LOCK = threading.RLock()
_cache: dict | None = None


_cache_stat = None


def _load_disk() -> dict:
    """Read the authoritative file, bypassing this process's cache.

    Mutations call this only while holding ``_process_lock``.  Reloading inside that lock is what
    turns a read/modify/write into one transaction across the app, worker and relay processes.
    """
    try:
        with open(_KEYFILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    data.setdefault("operator_nsec", None)
    data.setdefault("storage", {})
    return data


@contextmanager
def _process_lock():
    directory = os.path.dirname(_KEYFILE) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = _KEYFILE + ".lock"
    with open(lock_path, "a+") as lock:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _load() -> dict:
    global _cache, _cache_stat
    # Only TRUST a cache that actually loaded the operator key. `_load()` used to cache the FIRST read for the
    # life of the process — so a transient read failure (the keyfile being atomically os.replace()'d by a
    # sibling process at that instant, an FS hiccup, or the file not yet written on a racing startup) latched
    # an empty {} → operator_nsec=None FOREVER → that process could never get the operator key → hydrate_from_db
    # read 0 relay settings, so recording / bridge-token / every shareable setting silently fell back to
    # build-time DEFAULTS in that process (the "VODs don't save, hydrate returns 0" bug). Re-read until it loads.
    if _cache is not None and _cache.get("operator_nsec"):
        # …but a trusted cache is only trusted while the FILE hasn't moved. Three processes share
        # this file (app, worker, relay), and a life-of-the-process cache made each one blind to
        # keys the others minted — so each MINTED ITS OWN for the same user and overwrote the file
        # with it. Measured end state: the relay's gate full of storage pubkeys nobody derives,
        # every fresh account's writes refused "not in web of trust", and one user's documents
        # sealed under different keys per process. One stat() per read is the price of three
        # processes agreeing what a user's key is.
        try:
            st = os.stat(_KEYFILE)
            if _cache_stat == (st.st_mtime_ns, st.st_size):
                return _cache
        except OSError:
            return _cache
    data = _load_disk()
    # Don't cache a read that produced NO operator key — it may be transient/racing; a later call re-reads
    # and picks up the real key once it's there. Once the key is present, the cache is trusted (above).
    if data.get("operator_nsec"):
        _cache = data
        try:
            st = os.stat(_KEYFILE)
            _cache_stat = (st.st_mtime_ns, st.st_size)
        except OSError:
            _cache_stat = None
    return data


def _save(data: dict) -> None:
    global _cache, _cache_stat
    directory = os.path.dirname(_KEYFILE) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(_KEYFILE) + ".", suffix=".tmp",
                               dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _KEYFILE)
        # Persist the rename itself, not only the bytes inside the temporary file.
        try:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    _cache = data
    try:
        st = os.stat(_KEYFILE)
        _cache_stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        _cache_stat = None


# ---- per-user storage key (keyed by npub) ----
def get_storage_seckey(npub: str) -> bytes | None:
    if not npub:
        return None
    with _LOCK:
        hexsk = _load().get("storage", {}).get(npub)
    if hexsk:
        try:
            return bytes.fromhex(hexsk)
        except ValueError:
            return None
    return None


def set_storage_seckey(npub: str, sk: bytes) -> None:
    if not npub or not sk:
        return
    with _LOCK, _process_lock():
        data = _load_disk()
        data["storage"][npub] = sk.hex()
        _save(data)


# ---- operator key ----
def get_operator_nsec() -> str | None:
    with _LOCK:
        return _load().get("operator_nsec")


def set_operator_nsec(nsec: str) -> None:
    if not nsec:
        return
    with _LOCK, _process_lock():
        data = _load_disk()
        if data.get("operator_nsec") != nsec:
            data["operator_nsec"] = nsec
            _save(data)


# ---- fediverse-bridge derivation secret ----
def get_bridge_secret() -> bytes:
    """The stable HMAC secret the Nostr↔Fediverse bridge derives puppet keypairs from. Generated
    once on first use and persisted, so a given fedi account always maps to the same npub on this
    deployment (and a DB loss can't change anyone's bridged identity). Local-only by design — it must
    never leave the node (knowing it lets one forge every puppet's posts)."""
    with _LOCK, _process_lock():
        data = _load_disk()
        hexsec = data.get("bridge_secret")
        if not hexsec:
            hexsec = os.urandom(32).hex()
            data["bridge_secret"] = hexsec
            _save(data)
        try:
            return bytes.fromhex(hexsec)
        except ValueError:
            return hexsec.encode("utf-8")
