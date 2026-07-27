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
import threading

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_KEYFILE = os.environ.get("POSTERCHANAI_KEYFILE", os.path.join(_REPO_ROOT, "data", "keys.json"))
_LOCK = threading.RLock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    # Only TRUST a cache that actually loaded the operator key. `_load()` used to cache the FIRST read for the
    # life of the process — so a transient read failure (the keyfile being atomically os.replace()'d by a
    # sibling process at that instant, an FS hiccup, or the file not yet written on a racing startup) latched
    # an empty {} → operator_nsec=None FOREVER → that process could never get the operator key → hydrate_from_db
    # read 0 relay settings, so recording / bridge-token / every shareable setting silently fell back to
    # build-time DEFAULTS in that process (the "VODs don't save, hydrate returns 0" bug). Re-read until it loads.
    if _cache is not None and _cache.get("operator_nsec"):
        return _cache
    try:
        with open(_KEYFILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    data.setdefault("operator_nsec", None)
    data.setdefault("storage", {})
    # Don't cache a read that produced NO operator key — it may be transient/racing; a later call re-reads
    # and picks up the real key once it's there. Once the key is present, the cache is trusted (above).
    if data.get("operator_nsec"):
        _cache = data
    return data


def _save(data: dict) -> None:
    global _cache
    os.makedirs(os.path.dirname(_KEYFILE), exist_ok=True)
    tmp = _KEYFILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _KEYFILE)
    try:
        os.chmod(_KEYFILE, 0o600)
    except OSError:
        pass
    _cache = data


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
    with _LOCK:
        data = _load()
        data["storage"][npub] = sk.hex()
        _save(data)


# ---- operator key ----
def get_operator_nsec() -> str | None:
    with _LOCK:
        return _load().get("operator_nsec")


def set_operator_nsec(nsec: str) -> None:
    if not nsec:
        return
    with _LOCK:
        data = _load()
        if data.get("operator_nsec") != nsec:
            data["operator_nsec"] = nsec
            _save(data)


# ---- system-notifier key ----
def get_notifier_seckey() -> bytes:
    """The key this node sends SYSTEM notifications from (agent-run finished, uptime alerts, …).

    Deliberately NOT the operator key. On a single-admin deployment the operator key is very often the
    ADMIN'S OWN key — and a NIP-17 DM from you to you is a self-DM: every client (ours included) files
    it in your note-to-self thread as a message you sent, with no unread count and no notification. So
    the alert arrived, was decryptable, and told the user nothing. Sending from a distinct identity is
    what makes it a notification at all.

    Generated once and persisted, so the sender npub is stable — the user sees one "PosterChan"
    conversation instead of a new stranger per restart."""
    with _LOCK:
        data = _load()
        hexsec = data.get("notifier_seckey")
        if not hexsec:
            hexsec = os.urandom(32).hex()
            data["notifier_seckey"] = hexsec
            _save(data)
        return bytes.fromhex(hexsec)


# ---- fediverse-bridge derivation secret ----
def get_bridge_secret() -> bytes:
    """The stable HMAC secret the Nostr↔Fediverse bridge derives puppet keypairs from. Generated
    once on first use and persisted, so a given fedi account always maps to the same npub on this
    deployment (and a DB loss can't change anyone's bridged identity). Local-only by design — it must
    never leave the node (knowing it lets one forge every puppet's posts)."""
    with _LOCK:
        data = _load()
        hexsec = data.get("bridge_secret")
        if not hexsec:
            hexsec = os.urandom(32).hex()
            data["bridge_secret"] = hexsec
            _save(data)
        try:
            return bytes.fromhex(hexsec)
        except ValueError:
            return hexsec.encode("utf-8")
