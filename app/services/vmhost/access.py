"""`host.access.get` / `host.access.set`: who may use this host, managed from the client. ADMIN-only.

Deliberately NARROWER than Admin → VMs:
  * `set` writes ONLY `vmhost_allowed_npubs` (users). The ADMIN list is read-only here and is changed in
    the site's Admin panel, behind a site login. A key that can sign as one host admin must not be able to
    mint more host admins over Nostr — that is a privilege-escalation path with no second factor.
  * Every entry must be an npub or 64-hex public key. The whole request is REFUSED if any entry is not —
    silently dropping the unreadable line (what the settings parser does, correctly, for a hand-edited
    textarea) would tell the admin "saved" while the person they meant to add is not on the list. An
    `nsec` pasted by mistake is refused and never echoed back.
  * The write is DURABLE before success is reported: through `settings_store.write_through` (the same
    path Admin → VMs uses for this key), and only then applied to the running host. A lost write reads
    back as the old list after a restart — for a removal, somebody quietly regaining access.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

KEY = "vmhost_allowed_npubs"
MAX_ENTRIES = 500


def _err(code, msg):
    from .service import VmHostError
    return VmHostError(code, msg)


def npub(pk: str) -> str:
    try:
        from app.services.nostr import nostr_service
        return nostr_service.npub_of(pk)
    except Exception:
        return ""


def normalize_list(entries) -> list:
    """npub / hex → deduplicated lowercase hex, in order. Raises VmHostError on the first bad entry."""
    from .service import _to_hex
    if not isinstance(entries, list):
        raise _err("bad_request", "allowed must be a list of npubs")
    if len(entries) > MAX_ENTRIES:
        raise _err("bad_request", f"at most {MAX_ENTRIES} npubs")
    out, seen = [], set()
    for i, e in enumerate(entries):
        s = str(e or "").strip() if isinstance(e, str) else ""
        if s.lower().startswith("nsec"):
            raise _err("bad_request", f"entry {i + 1} is a SECRET key (nsec) — never share it; nothing was saved")
        pk = _to_hex(s)
        if not pk:
            raise _err("bad_request", f"entry {i + 1} is not an npub or a 64-hex public key — nothing was saved")
        if pk not in seen:
            seen.add(pk)
            out.append(pk)
    return out


async def default_writer(value: str) -> bool:
    """Durable write through the operator-signed settings document, then the in-process cache."""
    from app.database import SessionLocal
    from app.services import settings_store
    db = SessionLocal()
    try:
        wrote = await settings_store.write_through(db, {KEY: value})
    finally:
        db.close()
    if wrote != 1:
        return False
    settings_store.put(KEY, value, write_relay=False)
    return True


class AccessOps:
    def _access_writer(self):
        return getattr(self, "access_writer", None) or default_writer

    async def _op_host_access_get(self, pk, role, args, progress):
        return {"allowed": [{"pubkey": p, "npub": npub(p)} for p in self.cfg.allowed_pubkeys],
                "admins": [{"pubkey": p, "npub": npub(p)} for p in self.cfg.admin_pubkeys],
                "admins_editable": False,
                "note": "admins are changed in this site's Admin → VMs; assigned VMs grant access on their own"}

    async def _op_host_access_set(self, pk, role, args, progress):
        allowed = normalize_list(args.get("allowed"))
        value = "\n".join(npub(p) or p for p in allowed)
        try:
            ok = await self._access_writer()(value)
        except Exception as e:
            logger.warning("[vmhost] access list write failed: %s", e)
            ok = False
        if not ok:
            raise _err("backend_error", "the access list could not be saved durably — nothing changed")
        # Durable first, live second: the running host follows only a write that will survive a restart.
        self.cfg.allowed_pubkeys = list(allowed)
        logger.info("[vmhost] allowed users set to %d npub(s) by %s", len(allowed), pk[:12])
        return {"allowed": [{"pubkey": p, "npub": npub(p)} for p in allowed]}
