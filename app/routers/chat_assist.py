"""✨ in Telegram and Direct Messages -- thin; the logic is app/services/chat_assist_service.py."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
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
    action: str = "reply"         # reply | summarize | links | window | window_event | window_steps
    medium: str = "dm"            # telegram | dm
    messages: list[AssistMsg] = []
    count: int = 3
    probe: bool = False           # "would you answer me?" -- no model call
    windows: list[WindowCtx] = [] # action=window: what the ✨ panel collected
    instruction: str = ""         # action=window: what the person asked
    answer: str = ""              # action=window_event: the answer an event is looked for in, too
    today: str = ""               # action=window_event / window_steps: the person's local date, for "tomorrow"
    history: list[dict] = []      # action=window_steps: this panel's earlier requests, answers and done steps
    commands: bool = False        # action=window_steps: the window is a terminal the person may run commands in
    controls: list = []           # action=window_steps: the target window's visible controls, numbered by the client
    recipe: str = ""              # action=window_recipe: summary | tasks | draft | tidy | checklist
    text: str = ""                # action=window_recipe: the window's own text box (the note being tidied)
    reply_ref: Optional[int] = None   # action=window_recipe: the window's own Reply control, when its box is closed
    box_ref: Optional[int] = None     # action=window_recipe: the text box a tidied note replaces
    box_label: str = ""
    # action=window_feed: the timeline's posts on screen, in order. Bounded HERE: the service keeps the first
    # FEED_POSTS_MAX, but only after an unbounded body had been parsed and copied (code review, 2026-10-07).
    posts: list = Field(default_factory=list, max_length=200)
    target: int = 0               # action=window_feed recipe=reply: the post (1-based) to reply to
    subject: str = "posts"        # action=window_feed: "posts" (a timeline) or "notes" (the notebook)
    title: str = ""               # action=window_note: the open note's title


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
        if action == "window_steps":
            return {"ok": True, **(await svc.window_steps(
                db, user, [w.model_dump() for w in req.windows], req.instruction, req.history,
                req.commands, req.today, req.controls))}
        if action == "window_recipe":
            return {"ok": True, **(await svc.window_recipe(
                db, user, [w.model_dump() for w in req.windows], req.recipe, req.today, req.text,
                req.instruction, req.reply_ref, req.box_ref, req.box_label))}
        if action == "window_feed":
            return {"ok": True, **(await svc.window_feed(
                db, user, req.posts, req.recipe, req.instruction, req.target, req.subject))}
        if action == "window_calc":
            return {"ok": True, **(await svc.window_calc(db, user, req.instruction))}
        if action == "window_contact":
            return {"ok": True, **(await svc.window_contact(db, user, req.instruction))}
        if action == "window_note":
            return {"ok": True, **(await svc.window_note(db, user, req.recipe, req.title, req.text, req.instruction))}
        if action == "window":
            return {"ok": True, "answer": await svc.ask_window(
                db, user, [w.model_dump() for w in req.windows], req.instruction)}
        return {"ok": True, "links": await svc.summarize_links(db, user, items)}
    except svc.AssistError as e:
        return JSONResponse({"ok": False, "error": e.detail}, status_code=e.status)
