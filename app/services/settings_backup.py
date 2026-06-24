"""Settings disaster-recovery: back up every relay-stored setting to a JSON file and restore it.

Why this exists: settings live ONLY in the Nostr relay (`settings_store`). A relay wipe (purge bug,
a fresh Docker node seeding defaults over the shared store, etc.) silently replaces real values with
defaults — the LLM/bots/LB all break and the loss is invisible until something stops working. There
was no out-of-band backup: `nostr_relay_backup_datastore` (upstream DR broadcast) defaults OFF, and
`restore_from_upstream` can't win against newer re-seeded defaults (replaceable events, newest wins).

This module is the missing piece — a plain JSON snapshot you can take on a cron/before a deploy, plus
a restore that re-publishes the saved values with a FRESH timestamp (so they DO win over wiped
defaults) and a verify pass that proves the relay matches the backup afterward.

The pure helpers (serialize/deserialize/plan_restore/verify_restore) carry the DR semantics and are
unit-tested in tests/test_settings_backup_restore.py — no relay needed. The async fns are the thin
relay I/O (snapshot via settings_store._mig, write via settings_store.write_through).

CLI:
    python -m app.services.settings_backup backup  settings_backup.json
    python -m app.services.settings_backup restore settings_backup.json [--fill | --overwrite]
    python -m app.services.settings_backup verify  settings_backup.json
"""
from __future__ import annotations

import json
import os
from typing import Optional

# A snapshot is a small, human-diffable envelope so a restore can sanity-check what it's about to do.
_FORMAT = "posterchanai.settings-backup/1"


# ---- pure helpers (no I/O — unit-tested) ------------------------------------------------------

def serialize(settings: dict, *, created_at: Optional[float] = None, node: str = "") -> str:
    """settings dict → pretty, stable JSON envelope (sorted keys → clean git/file diffs)."""
    env = {
        "format": _FORMAT,
        "created_at": created_at,
        "node": node,
        "count": len(settings or {}),
        "settings": {k: ("" if v is None else str(v)) for k, v in (settings or {}).items()},
    }
    return json.dumps(env, indent=2, sort_keys=True)


def deserialize(blob: str) -> dict:
    """JSON envelope → the {key: value} settings dict. Tolerates a bare dict (no envelope)."""
    data = json.loads(blob)
    if isinstance(data, dict) and "settings" in data and isinstance(data["settings"], dict):
        return dict(data["settings"])
    if isinstance(data, dict):
        return dict(data)
    raise ValueError("not a settings backup: expected a JSON object")


def plan_restore(current: dict, backup: dict, *, mode: str = "fill") -> dict:
    """Compute the {key: value} to write so `current` matches `backup`. Two policies:

    - "fill" (default, SAFE): only restore keys that are MISSING or EMPTY in current but have a real
      (non-empty) value in the backup. This repairs a wipe without clobbering anything the running
      node legitimately changed AFTER the backup (e.g. a model path you just fixed in Admin).
    - "overwrite": restore every key whose backup value differs from current — a full rollback to the
      snapshot. Use only when you intend the backup to be authoritative.

    Never writes a key the backup doesn't contain (a restore can't delete). Returns {} when in sync."""
    if mode not in ("fill", "overwrite"):
        raise ValueError(f"unknown restore mode: {mode!r}")
    out = {}
    for k, bv in (backup or {}).items():
        bv = "" if bv is None else str(bv)
        cv = current.get(k)
        cv = None if cv is None else str(cv)
        if mode == "fill":
            if cv in (None, "") and bv != "":
                out[k] = bv
        else:  # overwrite
            if cv != bv:
                out[k] = bv
    return out


def verify_restore(current_after: dict, backup: dict, keys) -> list:
    """After a restore, confirm each restored key now equals the backup. Returns a list of
    (key, expected, actual) mismatches — empty list means the restore is provably complete."""
    bad = []
    for k in keys:
        exp = "" if backup.get(k) is None else str(backup.get(k))
        act = current_after.get(k)
        act = None if act is None else str(act)
        if act != exp:
            bad.append((k, exp, act))
    return bad


# ---- relay I/O (thin) -------------------------------------------------------------------------

def _bypass_proxy_env() -> None:
    """A side process inherits the app's HTTP/SOCKS proxy env, which routes the relay WebSocket
    through Tor → the local ws://127.0.0.1 handshake times out. Force-direct for relay I/O."""
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"


async def snapshot(db) -> dict:
    """Read EVERY shareable setting from the relay (authoritative). {key: value}."""
    from app.services import settings_store as ss
    op_sk = ss._OP_SK or ss._operator_seckey(db)
    if not op_sk:
        raise RuntimeError("no operator key — cannot read the relay")
    return await ss._mig.settings_all(ss._port(), op_sk) or {}


async def apply_restore(db, payload: dict) -> int:
    """Re-publish the given {key: value} to the relay with a fresh timestamp (so the values WIN over
    any wiped/re-seeded defaults). Returns the number of keys written. Mirrors an Admin save."""
    from app.services import settings_store as ss
    ss.ensure_operator_key(db)
    # reflect into the in-process cache too so a same-process verify sees them immediately
    for k, v in (payload or {}).items():
        ss._set_local(k, v)
    return await ss.write_through(db, dict(payload or {}))


# ---- CLI --------------------------------------------------------------------------------------

async def _cli(argv) -> int:
    import time
    from app.database import SessionLocal

    if len(argv) < 2 or argv[0] not in ("backup", "restore", "verify"):
        print(__doc__)
        return 2
    cmd, path = argv[0], argv[1]
    mode = "overwrite" if "--overwrite" in argv else "fill"
    _bypass_proxy_env()
    db = SessionLocal()
    try:
        if cmd == "backup":
            settings = await snapshot(db)
            with open(path, "w") as f:
                f.write(serialize(settings, created_at=time.time(), node=os.uname().nodename))
            print(f"backed up {len(settings)} settings → {path}")
            return 0

        with open(path) as f:
            backup = deserialize(f.read())
        current = await snapshot(db)

        if cmd == "verify":
            missing = plan_restore(current, backup, mode="overwrite")
            if not missing:
                print(f"OK — relay matches backup ({len(backup)} keys)")
                return 0
            print(f"DRIFT — {len(missing)} key(s) differ from backup:")
            for k in sorted(missing):
                print(f"  {k}: relay={current.get(k)!r}  backup={backup.get(k)!r}")
            return 1

        # restore
        payload = plan_restore(current, backup, mode=mode)
        if not payload:
            print("nothing to restore — relay already matches backup")
            return 0
        wrote = await apply_restore(db, payload)
        after = await snapshot(db)
        bad = verify_restore(after, backup, payload.keys())
        print(f"restored {wrote}/{len(payload)} key(s) [mode={mode}]; "
              f"verify: {'OK' if not bad else str(len(bad)) + ' MISMATCH'}")
        for k, exp, act in bad:
            print(f"  MISMATCH {k}: expected {exp!r}, got {act!r}")
        return 0 if not bad else 1
    finally:
        db.close()


if __name__ == "__main__":
    import asyncio
    import sys
    raise SystemExit(asyncio.run(_cli(sys.argv[1:])))
