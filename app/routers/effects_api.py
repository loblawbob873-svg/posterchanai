"""Direct Effects API (server-to-server), mirroring video_api / image_api.

Lets one posterchanai node hand an ffmpeg EFFECT command (glow, alive, nakedman, meme, …) to another
node in the unified `chat_server_urls` list. The receiving node runs the command LOCALLY through the
same CommandService the chat path uses, so there is exactly one effect implementation, and returns
the produced files as base64.

The peer needs ffmpeg and nothing else: no chat session, no user account, and no blob store — the
requesting node owns delivery and storage. See app/services/effects_factory.py.
"""
import base64
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.utils import lb_auth
from app.auth import get_current_user_optional

logger = logging.getLogger("effects_api")

router = APIRouter(prefix="/api", tags=["effects"])


async def get_effects_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> bool:
    """Accept load-balanced requests from other posterchanai nodes; otherwise API key / JWT.
    Mirrors video_api.get_video_auth."""
    # A peer node, proven by the shared secret once `lb_shared_secret` is set. The bare header
    # alone is settable by any caller — see app/utils/lb_auth.py.
    if lb_auth.is_internal(request):
        return True
    for token in (x_api_key, (authorization[7:] if authorization and authorization.startswith("Bearer ") else None)):
        if not token:
            continue
        try:
            from app.utils.auth_utils import query_api_key_with_retry, get_user_from_api_key
            api_key, user_id = query_api_key_with_retry(db, str(token).strip())
            if api_key and user_id and get_user_from_api_key(db, user_id):
                return True
        except Exception:
            pass
    try:
        if get_current_user_optional(request, db):
            return True
    except Exception:
        pass
    return True


class EffectFile(BaseModel):
    filename: Optional[str] = "file"
    content_type: Optional[str] = "application/octet-stream"
    data: str                                   # base64


class EffectRunRequest(BaseModel):
    command: str
    arg: Optional[str] = ""
    files: List[EffectFile] = []


@router.post("/effects/run")
async def run_effect(body: EffectRunRequest, request: Request,
                     _auth: bool = Depends(get_effects_auth), db: Session = Depends(get_db)):
    """Render one effect command here and return its files. Effects only — this must never become a
    general 'run any command on my node' hole, so the command is checked against the effect sets."""
    from app.services.command_service import CommandService

    command = (body.command or "").strip().lower()
    command = CommandService.COMMAND_ALIASES.get(command, command)
    allowed = set(CommandService.MOTION_EFFECTS) | set(CommandService.ANIMATED_EFFECTS)
    if command not in allowed:
        return {"error": "not an effect command"}

    attachments = []
    for f in body.files:
        try:
            raw = base64.b64decode(f.data or "")
        except Exception:
            continue
        if raw:
            attachments.append((f.filename or "file", raw, f.content_type or "application/octet-stream"))
    if not attachments:
        return {"error": "no input files"}

    cs = CommandService(db)
    # Never let a forwarded job forward again — that is the loop guard, and it is what keeps a
    # two-node fleet from bouncing one render back and forth.
    cs._effects_no_forward = True
    try:
        result = await cs._execute_command_inner(command, (body.arg or "").strip(),
                                                 None, None, attachments, None)
    except Exception as e:
        logger.warning("[EFFECTS] %s failed: %s", command, e)
        return {"error": "effect render failed"}

    if not isinstance(result, dict):
        return {"error": "effect produced no output"}
    if result.get("type") == "files" and result.get("files"):
        out = []
        for f in result["files"]:
            data = f.get("data") if isinstance(f, dict) else None
            if not data:
                continue
            out.append({"filename": f.get("filename") or "file",
                        "content_type": f.get("content_type") or "application/octet-stream",
                        "data": base64.b64encode(data).decode()})
        if out:
            return {"type": "files", "content": result.get("content") or "", "files": out}
        return {"error": "effect produced no output"}
    # A text answer ("attach an image", a refused modifier combo) is a real result — pass it back
    # so the requesting node doesn't pointlessly re-render it locally.
    if result.get("type") == "text":
        return {"type": "text", "content": result.get("content") or ""}
    return {"error": "effect produced no output"}
