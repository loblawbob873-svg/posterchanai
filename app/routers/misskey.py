"""Misskey integration endpoints — MiAuth flow and note posting."""

import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user
from app.services.misskey_service import check_miauth_session, build_miauth_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/misskey", tags=["misskey"])

# In-memory store: session_id → {user_id, instance_url}
# Cleared on restart; sessions expire naturally when Misskey rejects them.
_miauth_sessions: dict[str, dict] = {}


class MiAuthStartRequest(BaseModel):
    instance_url: str


class MiAuthStartResponse(BaseModel):
    session_id: str
    auth_url: str


@router.post("/miauth/start", response_model=MiAuthStartResponse)
async def start_miauth(
    data: MiAuthStartRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Generate a MiAuth session and return the authorization URL."""
    instance_url = data.instance_url.strip().rstrip("/")
    if not instance_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Instance URL must start with http:// or https://")

    session_id = str(uuid.uuid4())
    _miauth_sessions[session_id] = {
        "user_id": current_user.id,
        "instance_url": instance_url,
    }

    # Build callback URL pointing back at this server
    base_url = str(request.base_url).rstrip("/")
    callback_url = f"{base_url}/api/misskey/miauth/callback"

    auth_url = build_miauth_url(instance_url, session_id, callback_url)
    return MiAuthStartResponse(session_id=session_id, auth_url=auth_url)


@router.get("/miauth/callback")
async def miauth_callback(session: str, db: Session = Depends(get_db)):
    """Misskey redirects here after the user approves.  Exchange session for token."""
    session_data = _miauth_sessions.pop(session, None)
    if not session_data:
        return HTMLResponse(
            "<html><body><h2>Invalid or expired MiAuth session.</h2>"
            "<p>Please go back and try again.</p></body></html>",
            status_code=400,
        )

    user_id = session_data["user_id"]
    instance_url = session_data["instance_url"]

    try:
        result = await check_miauth_session(instance_url, session)
    except Exception as e:
        logger.error(f"MiAuth check failed: {e}")
        return HTMLResponse(
            "<html><body><h2>Authorization check failed.</h2>"
            f"<p>{e}</p><p>Please go back and try again.</p></body></html>",
            status_code=502,
        )

    if not result.get("ok"):
        return HTMLResponse(
            "<html><body><h2>Authorization was denied or not yet approved.</h2>"
            "<p>Please go back and try again.</p></body></html>",
            status_code=400,
        )

    token = result.get("token")
    if not token:
        return HTMLResponse(
            "<html><body><h2>No token returned by Misskey.</h2>"
            "<p>Please go back and try again.</p></body></html>",
            status_code=502,
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return HTMLResponse(
            "<html><body><h2>User not found.</h2></body></html>",
            status_code=404,
        )

    user.misskey_enabled = True
    user.misskey_instance_url = instance_url
    user.misskey_api_token = token
    db.commit()

    return HTMLResponse(
        "<html><head><style>"
        "body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;margin:0;background:#111;color:#eee;}"
        "div{text-align:center;} h2{color:#4caf50;}"
        "</style></head><body><div>"
        "<h2>✓ Misskey connected!</h2>"
        "<p>Your account has been linked to <strong>"
        + instance_url
        + "</strong>.</p>"
        "<p>You can close this tab and return to PosterChanAI.</p>"
        "<script>if(window.opener){window.opener.postMessage('misskey_connected','*');}"
        "setTimeout(()=>window.close(),3000);</script>"
        "</div></body></html>"
    )


@router.delete("/disconnect")
async def disconnect_misskey(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove Misskey credentials from the user's account."""
    current_user.misskey_enabled = False
    current_user.misskey_instance_url = None
    current_user.misskey_api_token = None
    db.commit()
    return {"ok": True, "message": "Misskey account disconnected"}
