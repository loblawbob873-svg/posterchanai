"""✨ in Telegram and Direct Messages -- thin; the logic is app/services/chat_assist_service.py."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user_optional
from app.database import get_db
from app.models import User
from app.services import chat_assist_service as svc
from app.services import texts_ai_service as texts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat-assist", tags=["chat-assist"])


class AssistMsg(BaseModel):
    me: bool = False
    text: str = ""
    who: str = ""                 # a group chat's speaker, for the summary only


class WindowCtx(BaseModel):
    title: str = ""
    view: str = ""
    kind: str = ""
    selection: str = ""
    text: str = ""


class AssistReq(BaseModel):
    action: str = "reply"         # reply | summarize | links | window
    medium: str = "dm"            # telegram | dm
    messages: list[AssistMsg] = []
    count: int = 3
    probe: bool = False           # "would you answer me?" -- no model call
    windows: list[WindowCtx] = [] # action=window: what the ✨ panel collected
    instruction: str = ""         # action=window: what the person asked
    answer: str = ""              # action=window_event: the answer an event is looked for in, too
    today: str = ""               # action=window_event: the person's local date, for "tomorrow"


@router.post("")
async def chat_assist(req: AssistReq, db: Session = Depends(get_db),
                      user: Optional[User] = Depends(get_current_user_optional)):
    if user is None:
        return JSONResponse({"ok": False, "error": "Sign in to PosterChan to use AI."}, status_code=401)
    may = await texts.allowed(user)
    if req.probe:
        return {"ok": True, "allowed": may}
    if not may:
        why = ("AI is turned off on this server." if texts.nostr_only() else
               "AI access is not enabled for this account — request access and an admin will approve.")
        return JSONResponse({"ok": False, "error": why}, status_code=403)
    action = (req.action or "").lower()
    if action not in svc.ACTIONS:
        return JSONResponse({"ok": False, "error": "Unknown action."}, status_code=400)
    items = [m.model_dump() for m in req.messages]
    medium = svc.medium_of(req.medium)
    try:
        if action == "reply":
            choices = await texts.draft_choices(db, user, items, req.count, medium)
            return {"ok": True, "content": choices[0], "choices": choices}
        if action == "summarize":
            return {"ok": True, "summary": await svc.summarize(db, user, items, medium)}
        if action == "window_event":
            return {"ok": True, "event": await svc.window_event(
                db, user, [w.model_dump() for w in req.windows], req.answer, req.today)}
        if action == "window":
            return {"ok": True, "answer": await svc.ask_window(
                db, user, [w.model_dump() for w in req.windows], req.instruction)}
        return {"ok": True, "links": await svc.summarize_links(db, user, items)}
    except svc.AssistError as e:
        return JSONResponse({"ok": False, "error": e.detail}, status_code=e.status)
