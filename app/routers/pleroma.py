"""Pleroma/Mastodon OAuth2 integration router."""

import html
import time
import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user
from app.services.pleroma_service import register_app, exchange_code, build_auth_url, verify_credentials

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
    scopes = "read write follow admin:read admin:write" if target == "bridge" else "read write"

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
            "<p>You can close this tab. Enable the bridge in Admin → Services and restart.</p>"
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
    db.commit()
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
    current_user.pleroma_notif_since = None
    db.commit()
    return {"ok": True}
