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
    if _cache is not None:
        return _cache
    try:
        with open(_KEYFILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    data.setdefault("operator_nsec", None)
    data.setdefault("storage", {})
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
