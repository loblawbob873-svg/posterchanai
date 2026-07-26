"""1-click Bridge Access: give a native Nostr user a fediverse presence + turn the bridge on.

Enabling, in one call (admin-token path on the operator's home instance = the Services read-account
instance):
  1. create a fediverse account for the user (Pleroma admin API), confirm/approve it, mint its token;
  2. copy the user's Nostr profile (kind-0 name/bio/avatar) onto that account;
  3. register their NIP-05 name on this instance if they don't have one;
  4. flip on both per-user toggles (fedi_bridge_enabled + fedi_crosspost_enabled) and mirror the
     account record to the relay.

Disabling just turns the toggles off (the fediverse account is kept). The bridge account's password
is stored per-user (UserSetting) so re-enabling can re-mint a token without recreating the account.
"""
import asyncio
import os
import json
import logging
import re
from urllib.parse import urlparse

import httpx

from app.services import settings_store, pleroma_service, users_store
from app.services.nostr import nostr_service
from app.services.nostr_store import _ws_query

logger = logging.getLogger(__name__)


def _g(key: str, default: str = "") -> str:
    v = settings_store.get(key, default)
    return v if v not in (None, "") else default


def _home_instance() -> str:
    return _g("fedi_bridge_instance_url", "").rstrip("/")


def _admin_token() -> str:
    # Dedicated admin token if set; otherwise fall back to the read-account token (works when that
    # account itself has admin rights on the instance).
    return _g("fedi_bridge_admin_token", "") or _g("fedi_bridge_access_token", "")


def _nip05_domain() -> str:
    return _g("nostr_relay_nip05_domain", "").strip().lstrip("@").lower()


def _port() -> int:
    return settings_store.get_int("nostr_relay_port", 3052)


def _sanitize_nick(s: str) -> str:
    """Fediverse nicknames + NIP-05 local-parts are conservative: keep [a-z0-9_], trim, cap length."""
    return re.sub(r"[^a-z0-9_]", "", (s or "").lower()).strip("_")[:30]


def _existing_nip05_name(pk: str) -> str | None:
    from app.services.nostr_relay.thread import _parse_nip05
    names, _ = _parse_nip05(settings_store.get("nostr_relay_nip05_names", "") or "", "")
    return next((n for n, h in names.items() if h == pk), None)


def _nickname_for(pk: str) -> str:
    base = _sanitize_nick(_existing_nip05_name(pk) or "")
    return base or ("u" + pk[:12])


async def _nostr_profile(pk: str) -> dict:
    try:
        evs = await _ws_query(_port(), [{"kinds": [0], "authors": [pk], "limit": 1}])
        if evs:
            return json.loads(evs[0].get("content", "{}")) or {}
    except Exception as e:
        logger.debug("[bridge-access] profile fetch failed: %s", e)
    return {}


async def _download(url: str, cap: int = 10_000_000) -> bytes | None:
    # SSRF guard: `url` is the caller's OWN kind-0 `picture`, i.e. fully attacker-controlled, and this
    # request is issued from inside the trust boundary (the relay, image servers and the
    # cloud metadata endpoint all live on loopback/link-local). Reuse the resolve-based check so a public
    # hostname that resolves to an internal IP is rejected too, and DON'T follow redirects — a 302 to
    # 127.0.0.1 would otherwise walk straight past the check.
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        from app.services import rss_service
        from starlette.concurrency import run_in_threadpool
        if not rss_service.looks_fetchable(url) or not await run_in_threadpool(rss_service.is_safe_host, url):
            logger.warning("[bridge-access] refused avatar fetch (unsafe host): %s", url[:120])
            return None
    except Exception:
        return None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
            r = await c.get(url, headers={"User-Agent": "posterchanai-bridge/1.0"})
        if r.status_code == 200 and len(r.content) <= cap:
            return r.content
    except Exception:
        pass
    return None


_nip05_lock = asyncio.Lock()   # the names blob is read-modify-written; two grants racing lost one


async def _ensure_nip05(db, pk: str, nickname: str) -> None:
    """Register `nickname` → this user's npub in the relay's NIP-05 names if they have none yet, so
    they're a verified local NIP-05 user (the bridge write-back/cross-post gate requires it)."""
    # Serialise: this is a read-modify-write of ONE shared settings blob with no re-read. Two grants
    # interleaving (or a grant racing client.py's claim_nip05) both read the same base and the second
    # put overwrote the first — one user silently ended up with no NIP-05 despite {"ok": true}.
    async with _nip05_lock:
        from app.services.nostr_relay.thread import _parse_nip05, trigger_nip05_reload
        raw = settings_store.get("nostr_relay_nip05_names", "") or ""
        names, _ = _parse_nip05(raw, "")
        if pk in names.values():
            return
        # avoid clobbering a name already taken by someone else
        name = nickname if nickname not in names else (nickname + pk[:4])
        npub = nostr_service.npub_of(pk)
        newraw = (raw.rstrip() + "\n" + f"{name} {npub}") if raw.strip() else f"{name} {npub}"
        settings_store.put("nostr_relay_nip05_names", newraw)
        try:
            await settings_store.write_through(db, {"nostr_relay_nip05_names": newraw})
            trigger_nip05_reload()
        except Exception as e:
            logger.debug("[bridge-access] nip05 register failed: %s", e)


async def enable(db, user, by_admin: bool = False) -> dict:
    """Provision (if needed) + enable bridge access for `user`. Returns {ok, error?}.

    `by_admin` gates SELF-SERVE. This function spends the OPERATOR's admin token: it creates an account
    on the home instance, force-confirms + approves it (bypassing that instance's manual approval), mints
    a read/write/follow token and turns on cross-posting. Two endpoints reach it — client.py's is
    admin-authenticated, auth.py's needed only a session, and /api/auth/nostr-login mints a session for
    ANY npub that can sign a challenge. So any passer-by could provision themselves an account on the
    operator's instance and federate through it. Gated here rather than in the router so no future caller
    can reintroduce the hole; self-serve is opt-in via `fedi_bridge_self_serve` (default OFF)."""
    if not by_admin:
        allow = (settings_store.get("fedi_bridge_self_serve", "") or "").strip().lower() in ("1", "true", "yes", "on")
        if not allow:
            return {"ok": False, "error": "Bridge access is granted by the admin on this instance."}
    inst, admin = _home_instance(), _admin_token()
    if not inst or not admin:
        return {"ok": False, "error": "Bridge home instance / admin token not configured (Admin → Services)."}
    pk = nostr_service.to_pubkey_hex(user.nostr_npub) if getattr(user, "nostr_npub", None) else None
    if not pk:
        return {"ok": False, "error": "Your account has no linked Nostr key."}

    # The user's Nostr profile (kind-0) — drives both the fediverse nickname and the copied profile.
    prof = await _nostr_profile(pk)
    # Nickname: prefer their Nostr profile name, then an existing NIP-05 name, else npub-derived.
    nickname = (_sanitize_nick(prof.get("name") or prof.get("display_name") or "")
                or _nickname_for(pk))

    # 1. Ensure a linked fediverse account (create it the first time).
    if not (user.pleroma_enabled and user.pleroma_access_token and user.pleroma_instance_url):
        from app.models import UserSetting
        prow = (db.query(UserSetting)
                .filter(UserSetting.user_id == user.id, UserSetting.key == "fedi_bridge_pw").first())
        password = (prow.value if prow and prow.value else None) or os.urandom(16).hex()
        domain = _nip05_domain() or urlparse(inst).netloc.split(":")[0]
        email = f"{nickname}.bridge@{domain}"
        r = await pleroma_service.admin_create_user(inst, admin, nickname, email, password)
        if not r.get("ok"):
            return {"ok": False, "error": "Could not create fediverse account: " + (r.get("error") or "")}
        # ONLY approve an account we actually created this call. `nickname` is derived from the
        # requester's own kind-0, and admin_create_user reports ok for an already-taken nickname — so
        # approving unconditionally force-approved arbitrary pending registrations on the operator's
        # instance. An existing account either already belongs to this user (the token mint below
        # succeeds) or isn't ours to touch.
        if r.get("created"):
            await pleroma_service.admin_confirm_approve(inst, admin, nickname)
        try:
            token = await pleroma_service.password_grant(inst, nickname, password, scopes="read write follow")
        except Exception as e:
            return {"ok": False, "error": f"Account created but token mint failed ({e}). "
                                          f"If the nickname '{nickname}' already exists with a different "
                                          f"password, that's the cause."}
        user.pleroma_instance_url = inst
        user.pleroma_access_token = token
        user.pleroma_enabled = True
        if prow:
            prow.value = password
        else:
            db.add(UserSetting(user_id=user.id, key="fedi_bridge_pw", value=password))
        db.commit()

        # 2. Copy the Nostr profile onto the new account (best-effort).
        avatar = await _download(prof.get("picture") or "")
        try:
            await pleroma_service.update_credentials(
                inst, token,
                display_name=(prof.get("display_name") or prof.get("name") or nickname),
                note=prof.get("about") or "", avatar_bytes=avatar)
        except Exception as e:
            logger.debug("[bridge-access] profile copy failed: %s", e)

    # 3. NIP-05 name (verified local identity — required by the write-back/cross-post gate).
    await _ensure_nip05(db, pk, nickname)

    # 4. Flip on both per-user toggles + mirror to the relay.
    user.fedi_bridge_enabled = True
    user.fedi_crosspost_enabled = True
    db.commit()
    try:
        await users_store.sync_user(db, user, force=True)
    except Exception as e:
        logger.debug("[bridge-access] user sync failed: %s", e)
    return {"ok": True, "instance": inst, "nickname": nickname}


async def disable(db, user) -> dict:
    """Turn bridge access off (keeps the fediverse account; just stops mirroring + cross-posting)."""
    user.fedi_bridge_enabled = False
    user.fedi_crosspost_enabled = False
    db.commit()
    try:
        await users_store.sync_user(db, user, force=True)
    except Exception as e:
        logger.debug("[bridge-access] user sync failed: %s", e)
    return {"ok": True}
