"""Texts (SMS/MMS) — server help for the conversation screen. Thin: the logic is
app/services/texts_ai_service.py.

TWO WAYS IN, ONE GATE. The web client (browser, desktop, tablet) carries the app session like every
other authed call. The Android native conversation screen (ThreadActivity) has no session — it has
the account key sealed in the keystore, the same key its archive and outbox already sign with — so
it proves ownership with a kind-27235 event bound to this endpoint's purpose (`texts-ai-reply`),
exactly as /client/files-index and /client/sync-manifest are called. Either way the answer to "may
this account use AI?" is `texts_ai_service.allowed`, which is `nip05_access`'s predicate.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user_optional
from app.database import get_db
from app.models import User
from app.services import texts_ai_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/texts", tags=["texts"])

AUTH_PURPOSE = "texts-ai-reply"


class TextsMsg(BaseModel):
    me: bool = False
    text: str = ""


class TextsAiReplyReq(BaseModel):
    messages: list[TextsMsg] = []
    probe: bool = False              # "would you answer me?" — no model call
    pubkey: Optional[str] = None     # native: the key that signed `auth`
    auth: Optional[str] = None       # native: base64 kind-27235, content == AUTH_PURPOSE


def _who(req: TextsAiReplyReq, session_user, db):
    """(user or None, pubkey hex or "", authenticated?)"""
    if session_user is not None:
        return session_user, "", True
    if req.pubkey and req.auth:
        from app.services.nostr import nostr_service, event as nostr_event
        pk = nostr_service.to_pubkey_hex(req.pubkey) or ""
        if pk and nostr_event.verify_self_auth(req.auth, pk, AUTH_PURPOSE):
            user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first() if db else None
            return user, pk, True
    return None, "", False


@router.post("/ai-reply")
async def texts_ai_reply(req: TextsAiReplyReq, db: Session = Depends(get_db),
                         session_user: Optional[User] = Depends(get_current_user_optional)):
    """✨ in a Texts conversation: a suggested reply to the last message, for the composer."""
    user, pk, ok = _who(req, session_user, db)
    if not ok:
        return JSONResponse({"ok": False, "error": "Sign in to PosterChan to use AI replies."}, status_code=401)
    may = await svc.allowed(user, pk)
    if req.probe:
        return {"ok": True, "allowed": may}
    if not may:
        why = ("AI is turned off on this server." if svc.nostr_only() else
               "AI access is not enabled for this account — request access and an admin will approve.")
        return JSONResponse({"ok": False, "error": why}, status_code=403)
    try:
        draft = await svc.draft_reply(db, user, req.messages)
    except svc.TextsAiError as e:
        return JSONResponse({"ok": False, "error": e.detail}, status_code=e.status)
    return {"ok": True, "content": draft}
