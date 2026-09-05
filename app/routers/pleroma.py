"""Pleroma/Mastodon OAuth2 integration router."""

import html
import time
import uuid
import asyncio
import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user
from app.services.pleroma_service import (register_app, exchange_code, build_auth_url, verify_credentials,
                                          resolve_account, follow_account)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pleroma", tags=["pleroma"])

# In-memory pending OAuth states: state_token → {user_id, instance_url, client_id, client_secret, redirect_uri, created_at}
_oauth_states: dict[str, dict] = {}
_OAUTH_STATE_TTL = 3600  # 1 hour


def _evict_expired_states() -> None:
    """Remove OAuth states older than _OAUTH_STATE_TTL seconds."""
    cutoff = time.time() - _OAUTH_STATE_TTL
    expired = [k for k, v in _oauth_states.items() if v.get("created_at", 0) < cutoff]
    for k in expired:
        _oauth_states.pop(k, None)


class PleromaOAuthStartRequest(BaseModel):
    instance_url: str
    target: str = "user"   # "user" = link the caller's account; "bridge" = the global bridge read account (admin only)


class PleromaOAuthStartResponse(BaseModel):
    auth_url: str


@router.post("/oauth/start", response_model=PleromaOAuthStartResponse)
async def start_oauth(
    data: PleromaOAuthStartRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Register PosterChanAI as an app on the Pleroma instance and return the auth URL."""
    _evict_expired_states()  # prune stale pending flows before adding a new one

    instance_url = data.instance_url.strip().rstrip("/")
    if not instance_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Instance URL must start with http:// or https://")

    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/api/pleroma/oauth/callback"

    target = "bridge" if (data.target or "user").strip().lower() == "bridge" else "user"
    if target == "bridge" and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    # The bridge read account doubles as the admin account used to create fediverse accounts for new
    # bridge users, so request admin scopes — otherwise the token can't call the Pleroma admin API
    # ("Insufficient permissions: admin:read:accounts"). A non-admin account just won't be granted them.
    scopes = "read write follow admin:read admin:write" if target == "bridge" else "read write follow"

    try:
        app_data = await register_app(instance_url, redirect_uri, scopes=scopes)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not register app with instance: {e}")

    client_id = app_data.get("client_id")
    client_secret = app_data.get("client_secret")
    if not client_id or not client_secret:
        raise HTTPException(status_code=502, detail="Instance did not return client credentials")

    state = str(uuid.uuid4())
    _oauth_states[state] = {
        "user_id": current_user.id,
        "instance_url": instance_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "target": target,
        "created_at": time.time(),
    }

    auth_url = build_auth_url(instance_url, client_id, redirect_uri, scopes=scopes) + f"&state={state}"
    return PleromaOAuthStartResponse(auth_url=auth_url)


@router.get("/oauth/callback")
async def oauth_callback(code: str = None, state: str = None, error: str = None, db: Session = Depends(get_db)):
    """Pleroma redirects here after the user approves. Exchange code for access token."""

    def _error_page(msg: str) -> HTMLResponse:
        safe_msg = html.escape(msg)
        return HTMLResponse(
            "<html><head><style>"
            "body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;"
            "min-height:100vh;margin:0;background:#111;color:#eee;}"
            "div{text-align:center;} h2{color:#f44;}"
            "</style></head><body><div>"
            f"<h2>❌ Authorization failed</h2><p>{safe_msg}</p>"
            "<p>Please close this tab and try again.</p>"
            "</div></body></html>",
            status_code=400,
        )

    if error:
        return _error_page(f"Instance returned: {error}")

    if not state or not code:
        return _error_page("Missing state or code parameter.")

    pending = _oauth_states.pop(state, None)
    if not pending:
        return _error_page("Invalid or expired OAuth state. Please start again.")

    try:
        access_token = await exchange_code(
            instance_url=pending["instance_url"],
            client_id=pending["client_id"],
            client_secret=pending["client_secret"],
            redirect_uri=pending["redirect_uri"],
            code=code,
        )
    except Exception as e:
        logger.error(f"Pleroma token exchange failed: {e}")
        return _error_page(f"Token exchange failed: {e}")

    # Verify the token works and get username
    account = {}
    try:
        account = await verify_credentials(pending["instance_url"], access_token)
        display = account.get("username") or account.get("acct") or "unknown"
    except Exception as e:
        logger.warning(f"Could not verify Pleroma credentials after token exchange: {e}")
        display = "unknown"

    instance_url = pending["instance_url"]

    # Bridge read account (admin): save into the GLOBAL fedi_bridge_* settings instead of a user, so
    # the Nostr↔Fediverse global-timeline mirror reads through it. One click, no token pasting.
    if pending.get("target") == "bridge":
        from app.services import settings_store
        settings_store.put("fedi_bridge_instance_url", instance_url)
        settings_store.put("fedi_bridge_access_token", access_token)
        # Persist SYNCHRONOUSLY to the relay (not the fire-and-forget put above) so the token is
        # durably saved + hydrated on restart — a dropped background write is how it went missing.
        try:
            await settings_store.write_through(db, {
                "fedi_bridge_instance_url": instance_url,
                "fedi_bridge_access_token": access_token,
            })
        except Exception as e:
            logger.warning(f"[pleroma] bridge token write-through failed: {e}")
        safe_display = html.escape(display)
        safe_instance = html.escape(instance_url)
        return HTMLResponse(
            "<html><head><style>"
            "body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;"
            "min-height:100vh;margin:0;background:#111;color:#eee;}"
            "div{text-align:center;} h2{color:#4caf50;}"
            "</style></head><body><div>"
            "<h2>✓ Bridge read account connected!</h2>"
            f"<p>Reading as <strong>@{safe_display}</strong> on <strong>{safe_instance}</strong>.</p>"
            "<p>You can close this tab. Enable the bridge in Admin → Social and restart.</p>"
            "<script>if(window.opener){window.opener.postMessage('pleroma_bridge_connected','*');}"
            "setTimeout(()=>window.close(),3000);</script>"
            "</div></body></html>"
        )

    user = db.query(User).filter(User.id == pending["user_id"]).first()
    if not user:
        return _error_page("User not found.")

    user.pleroma_enabled = True
    user.pleroma_instance_url = pending["instance_url"]
    user.pleroma_access_token = access_token
    from app.services.fedi_bridge_identity import acct_of
    from urllib.parse import urlparse
    user.pleroma_acct = acct_of(account, urlparse(pending["instance_url"]).netloc).lower() or None
    db.commit()
    # One-time: mirror the bridged accounts the user already follows on Nostr onto their new Pleroma.
    _fire_backfill(pending["user_id"])
    # Escape values that came from user input / the remote instance before putting them in HTML.
    safe_display = html.escape(display)
    safe_instance = html.escape(instance_url)
    return HTMLResponse(
        "<html><head><style>"
        "body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;margin:0;background:#111;color:#eee;}"
        "div{text-align:center;} h2{color:#4caf50;}"
        "</style></head><body><div>"
        "<h2>✓ Pleroma connected!</h2>"
        f"<p>Logged in as <strong>@{safe_display}</strong> on <strong>{safe_instance}</strong>.</p>"
        "<p>You can close this tab and return to PosterChanAI.</p>"
        "<script>if(window.opener){{window.opener.postMessage('pleroma_connected','*');}}"
        "setTimeout(()=>window.close(),3000);</script>"
        "</div></body></html>"
    )


@router.post("/disconnect")
async def disconnect_pleroma(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove Pleroma credentials from the user account.

    Fully clears per-platform state so nothing stale survives a disconnect (the
    notification cursor included) — a later reconnect starts clean."""
    current_user.pleroma_enabled = False
    current_user.pleroma_instance_url = None
    current_user.pleroma_access_token = None
    current_user.pleroma_acct = None
    current_user.pleroma_notif_since = None
    # Clear the once-flag so a future (re)connect re-runs the follow-list backfill (e.g. a different account).
    from app.models import UserSetting
    db.query(UserSetting).filter(UserSetting.user_id == current_user.id,
                                 UserSetting.key == "pleroma_followlist_backfilled").delete()
    db.commit()
    return {"ok": True}


# ---- one-time backfill: on connect, follow the bridged accounts the user ALREADY follows on Nostr ----
_backfill_tasks: set = set()      # strong refs so fire-and-forget tasks aren't GC'd mid-run
_backfill_inflight: set = set()   # user_ids with a run in flight — a double OAuth callback can't double-run
_BACKFILL_MAX_FOLLOWS = 2000      # bound the scan (a huge follow list won't hammer the instance)


def _proxy_actor(ev: dict) -> str | None:
    """The AP actor URL from a kind-0's NIP-48 proxy tag — only when protocol (t[2]) is activitypub."""
    for t in ev.get("tags") or []:
        if len(t) >= 3 and t[0] == "proxy" and t[1] and (t[2] or "").lower() == "activitypub":
            return t[1]
    return None


_BACKFILL_FLAG = "pleroma_followlist_backfilled"


async def _backfill_bridged_follows(user_id: int) -> None:
    """One-time per connection: follow, on the user's just-linked Pleroma, every BRIDGED account they
    ALREADY follow on Nostr — so existing follows are covered, not just new ones (the live toggleFollow
    hook). Best-effort, paced, bounded. Only touches accounts the user already deliberately follows.

    Session discipline: read creds in a SHORT session and release it BEFORE the long paced loop (so no
    Postgres transaction is held idle past its timeout), then persist the once-flag in a FRESH session —
    and ONLY on a confidently-complete run that's still the SAME connection (guards against a transient
    relay read, a scope-denied token, or a disconnect/reconnect mid-run wrongly marking it done)."""
    from app.database import SessionLocal
    from app.models import UserSetting
    from app.services import settings_store
    from app.services.nostr import nostr_service
    from app.services import fedi_bridge_identity as ident
    from app.services.nostr_store import _ws_query
    try:
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if not user or not (user.pleroma_instance_url and user.pleroma_access_token):
                return
            if db.query(UserSetting).filter(UserSetting.user_id == user_id,
                                            UserSetting.key == _BACKFILL_FLAG, UserSetting.value == "1").first():
                return   # already backfilled for this connection
            inst, tok = user.pleroma_instance_url, user.pleroma_access_token
            pk = nostr_service.to_pubkey_hex(user.nostr_npub) if user.nostr_npub else None
        finally:
            db.close()                              # release the read session BEFORE the long loop
        if not pk:
            return
        try:
            port = int(settings_store.get("nostr_relay_port", "3052") or "3052")
        except (TypeError, ValueError):
            port = 3052

        ok, k3 = await ident.query_one(port, {"authors": [pk], "kinds": [3], "limit": 1})
        if not ok:
            return   # relay read failed → do NOT mark done; a later reconnect retries
        followed = list(dict.fromkeys(
            t[1] for t in ((k3 or {}).get("tags") or []) if t and t[0] == "p" and len(t) >= 2 and t[1]
        ))[:_BACKFILL_MAX_FOLLOWS]
        # AP actor URLs of the bridged accounts among the follows (kind-0 read in author batches).
        actors, got_k0 = [], False
        for i in range(0, len(followed), 200):
            evs = await _ws_query(port, [{"kinds": [0], "authors": followed[i:i + 200]}]) or []
            if evs:
                got_k0 = True
            for ev in evs:
                a = _proxy_actor(ev)
                if a:
                    actors.append(a)
        actors = list(dict.fromkeys(actors))
        # If the user HAS follows but zero kind-0s came back, the relay read likely failed (not "no
        # bridged accounts") → don't mark done, retry next connect.
        read_ok = (not followed) or got_k0

        done, scope_denied = 0, False
        for i, actor in enumerate(actors):
            try:
                acct = await resolve_account(inst, tok, actor)
                if acct and acct.get("id"):
                    await follow_account(inst, tok, acct["id"])
                    done += 1
            except httpx.HTTPStatusError as e:
                if e.response is not None and e.response.status_code in (401, 403):
                    scope_denied = True   # token can't follow → stop; the reconnect that grants it re-runs
                    break
            except Exception:
                pass   # best-effort per account
            if i % 10 == 9:
                await asyncio.sleep(1)   # pace so a big list doesn't burst the instance

        # Persist the once-flag ONLY on a confidently-complete run, in a FRESH session, and only if the
        # connection is unchanged (same token → not disconnected/reconnected mid-run, which re-fired its own).
        if read_ok and not scope_denied:
            db2 = SessionLocal()
            try:
                u = db2.query(User).filter(User.id == user_id).first()
                if u and u.pleroma_access_token == tok:
                    row = (db2.query(UserSetting)
                           .filter(UserSetting.user_id == user_id, UserSetting.key == _BACKFILL_FLAG).first())
                    if row:
                        row.value = "1"
                    else:
                        db2.add(UserSetting(user_id=user_id, key=_BACKFILL_FLAG, value="1"))
                    db2.commit()
            finally:
                db2.close()
        logger.info("[pleroma] follow-list backfill: followed %d bridged account(s) for user %d (done=%s)",
                    done, user_id, read_ok and not scope_denied)
    except Exception as e:
        logger.warning("[pleroma] follow-list backfill failed: %s", e)


def _fire_backfill(user_id: int) -> None:
    if user_id in _backfill_inflight:
        return                                       # a run for this user is already in flight (double callback)
    _backfill_inflight.add(user_id)

    async def _run():
        try:
            await _backfill_bridged_follows(user_id)
        finally:
            _backfill_inflight.discard(user_id)
    t = asyncio.ensure_future(_run())
    _backfill_tasks.add(t)
    t.add_done_callback(_backfill_tasks.discard)


class FollowBridgedReq(BaseModel):
    actor: str = ""


@router.post("/follow-bridged")
async def follow_bridged(data: FollowBridgedReq, current_user: User = Depends(get_current_user)):
    """Follow, on the user's linked Pleroma, the real fediverse account behind a bridged Nostr account.
    The web client calls this (opt-in) when the user follows a bridged, proxy-tagged account on Nostr,
    passing the account's AP actor URL. Best-effort: every failure returns {ok:false, error} — the
    Nostr follow already happened client-side and must not be undone by a Pleroma hiccup."""
    actor = (data.actor or "").strip()
    if not actor:
        return {"ok": False, "error": "no account"}
    inst, tok = current_user.pleroma_instance_url, current_user.pleroma_access_token
    if not (inst and tok):
        return {"ok": False, "connected": False, "error": "Pleroma not connected"}
    try:
        acct = await resolve_account(inst, tok, actor)
        if not acct or not acct.get("id"):
            return {"ok": False, "error": "couldn't find that account on your instance"}
        await follow_account(inst, tok, acct["id"])
    except httpx.HTTPStatusError as e:
        # 401 (expired token) / 403 (token lacks the `follow` scope, granted only on a fresh connect)
        # → tell the user to reconnect ONCE, from either the resolve or the follow call.
        if e.response is not None and e.response.status_code in (401, 403):
            return {"ok": False, "needs_reconnect": True,
                    "error": "reconnect Pleroma to grant follow permission"}
        logger.warning("[pleroma] follow-bridged failed: %s", e)
        return {"ok": False, "error": "follow failed"}
    except Exception as e:
        logger.warning("[pleroma] follow-bridged failed: %s", e)
        return {"ok": False, "error": "could not reach your Pleroma instance"}
    return {"ok": True, "acct": acct.get("acct") or acct.get("username") or ""}


class SocialPublishReq(BaseModel):
    event: dict
    broadcast_only: bool = False


@router.post("/social-publish")
async def publish_social(data: SocialPublishReq, current_user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """Route signed social activity according to the authenticated account's current setting."""
    import json
    from app.services.fedi_only_service import route, SOCIAL_KINDS
    if len(json.dumps(data.event)) > 256_000:
        raise HTTPException(status_code=413, detail="Social event is too large")
    if data.event.get("kind") not in SOCIAL_KINDS:
        raise HTTPException(status_code=400, detail="Not a social event")
    return await route(db, current_user, data.event, broadcast_only=data.broadcast_only)


@router.get("/private-events")
def private_social_history(before: int | None = None, before_id: str | None = None,
                           limit: int = 200, current_user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """Only this account's Fediverse-only history; timestamp+id cursor avoids burst truncation."""
    import json
    from sqlalchemy import or_, and_
    from app.models import FediOnlyEvent
    q = db.query(FediOnlyEvent).filter(FediOnlyEvent.user_id == current_user.id,
                                      FediOnlyEvent.deleted.is_(False))
    if before is not None:
        q = q.filter(or_(FediOnlyEvent.created_at < before,
                        and_(FediOnlyEvent.created_at == before, FediOnlyEvent.id < (before_id or ""))))
    rows = q.order_by(FediOnlyEvent.created_at.desc(), FediOnlyEvent.id.desc()).limit(max(1,min(limit,500))).all()
    return {"events": [json.loads(row.raw) for row in rows]}
