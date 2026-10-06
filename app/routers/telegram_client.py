"""/api/tgc — the Telegram client's API (app/services/telegram_client). Members of this instance only,
each seeing only their own Telegram. Routers stay thin: every rule lives in the manager."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_current_user_optional
from app.database import SessionLocal, get_db
from app.models import User
from app.services import instance_membership
from app.services.telegram_client.manager import TGError, manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tgc", tags=["telegram-client"])


async def member(user=Depends(get_current_user)):
    return await instance_membership.require_user(user)


# MEDIA TICKETS. An <img>/<video>/<audio> cannot send an Authorization header, and in the APK and the
# desktop bundle the page is not on this origin, so the session cookie does not ride along either.
# The long-lived session token must not go in a URL (it lands in logs and history), so the client
# asks for a random ticket that only opens Telegram media, for that one account, for a few hours.
import secrets as _secrets
import time as _time

_TICKETS: dict = {}
_TICKET_TTL = 6 * 3600


def _mint(user_id: int) -> str:
    now = _time.time()
    for k, (exp, _u) in list(_TICKETS.items()):
        if exp < now:
            _TICKETS.pop(k, None)
    t = _secrets.token_urlsafe(24)
    _TICKETS[t] = (now + _TICKET_TTL, user_id)
    return t


async def media_user(t: str = "", db: Session = Depends(get_db), user=Depends(get_current_user_optional)):
    """The account a media request is for: a valid ticket, else an ordinary session."""
    hit = _TICKETS.get(t or "")
    if hit and hit[0] > _time.time():
        u = db.query(User).filter(User.id == hit[1]).first()
        if u is not None:
            return u
    if user is None:
        raise HTTPException(401, "Not signed in")
    return await instance_membership.require_user(user)


def _err(e: TGError, code: int = 400):
    return JSONResponse({"ok": False, "error": str(e)}, status_code=code)


class Phone(BaseModel):
    phone: str


class Code(BaseModel):
    code: str


class Password(BaseModel):
    password: str


class Send(BaseModel):
    chat_id: int
    text: str
    reply_to: int = 0


class Read(BaseModel):
    chat_id: int
    max_id: int = 0


@router.get("/status")
async def status(db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        return await manager().status(db, user)
    except TGError as e:
        return _err(e)


@router.get("/ticket")
async def ticket(user: User = Depends(member)):
    return {"t": _mint(user.id), "ttl": _TICKET_TTL}


@router.post("/login/phone")
async def login_phone(req: Phone, db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        return await manager().send_code(db, user, req.phone)
    except TGError as e:
        return _err(e)


@router.post("/login/code")
async def login_code(req: Code, db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        return await manager().sign_in_code(db, user, req.code)
    except TGError as e:
        return _err(e)


@router.post("/login/password")
async def login_password(req: Password, db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        return await manager().sign_in_password(db, user, req.password)
    except TGError as e:
        return _err(e)


@router.post("/logout")
async def logout(db: Session = Depends(get_db), user: User = Depends(member)):
    return await manager().logout(db, user)


@router.get("/dialogs")
async def dialogs(limit: int = 100, db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        return {"ok": True, "dialogs": await manager().dialogs(db, user, limit)}
    except TGError as e:
        return _err(e)


@router.get("/messages/{chat_id}")
async def messages(chat_id: int, before: int = 0, limit: int = 50,
                   db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        msgs = await manager().messages(db, user, chat_id, before, limit)
        # The first page also says how far the other side has read (the ✓✓ line); older pages need not.
        read_out = 0 if before else await manager().read_out(db, user, chat_id)
        return {"ok": True, "messages": msgs, "read_out": read_out}
    except TGError as e:
        return _err(e)


@router.post("/send")
async def send(req: Send, db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        return {"ok": True, "message": await manager().send_text(db, user, req.chat_id, req.text, req.reply_to)}
    except TGError as e:
        return _err(e)


@router.post("/send-file")
async def send_file(chat_id: int = Form(...), caption: str = Form(""), mode: str = Form("auto"),
                    reply_to: int = Form(0), file: UploadFile = File(...),
                    db: Session = Depends(get_db), user: User = Depends(member)):
    from app.services.telegram_client.manager import MAX_UPLOAD
    data = await file.read(MAX_UPLOAD + 1)
    try:
        m = await manager().send_file(db, user, chat_id, data, file.filename or "file", caption,
                                      mode if mode in ("auto", "document", "voice", "video_note") else "auto",
                                      reply_to)
        return {"ok": True, "message": m}
    except TGError as e:
        return _err(e)


class React(BaseModel):
    chat_id: int
    msg_id: int
    emoji: str


@router.post("/react")
async def react(req: React, db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        return {"ok": True, "reactions": await manager().react(db, user, req.chat_id, req.msg_id, req.emoji)}
    except TGError as e:
        return _err(e)


@router.get("/search")
async def search(q: str = "", db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        return {"ok": True, "results": await manager().search(db, user, q)}
    except TGError as e:
        return _err(e)


@router.post("/read")
async def read(req: Read, db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        await manager().mark_read(db, user, req.chat_id, req.max_id)
        return {"ok": True}
    except TGError as e:
        return _err(e)


@router.get("/media/{chat_id}/{msg_id}")
async def media(chat_id: int, msg_id: int, thumb: int = 0, db: Session = Depends(get_db),
                user: User = Depends(media_user)):
    try:
        data, mime, name = await manager().media(db, user, chat_id, msg_id, bool(thumb))
    except TGError as e:
        raise HTTPException(404, str(e))
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "._- ")[:120] or "file"
    # Somebody's attachment, served from OUR origin: never let the browser render it as a page.
    return Response(data, media_type=mime if not mime.startswith(("text/html", "application/xhtml")) else "text/plain",
                    headers={"Content-Disposition": f'inline; filename="{safe}"',
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"})


@router.get("/avatar/{peer_id}")
async def avatar(peer_id: int, db: Session = Depends(get_db), user: User = Depends(media_user)):
    try:
        data = await manager().avatar(db, user, peer_id)
    except TGError:
        data = b""
    if not data:
        return Response(status_code=204)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600",
                                                            "X-Content-Type-Options": "nosniff"})


def _release(db):
    """Close a websocket's setup session; never raise out of a handler's teardown (a connection Postgres
    already dropped makes the close's rollback fail, and that failure is not the socket's business)."""
    if db is not None:
        try:
            db.close()
        except Exception as e:
            logger.debug("[tgc] session close after a dropped connection: %s", type(e).__name__)
    return None


@router.websocket("/ws")
async def events(ws: WebSocket):
    """Live messages. The session token arrives in the FIRST FRAME (never the URL, where it would be
    written into every proxy and access log), then this socket carries only that account's events."""
    from app.auth import decode_token
    await ws.accept()
    db = SessionLocal()
    q = None
    user = None
    try:
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=15))
        except Exception:
            await ws.close(code=4401)
            return
        payload = decode_token(str(first.get("token") or "")) if isinstance(first, dict) else None
        uid = payload.get("sub") if payload else None
        user = db.query(User).filter(User.id == int(uid)).first() if str(uid or "").isdigit() else None
        if user is None:
            await ws.close(code=4401)
            return
        try:
            await instance_membership.require_user(user)
        except Exception:
            await ws.close(code=4403)
            return
        st = await manager().status(db, user)
        q = manager().subscribe(user.id)
        # THE SESSION IS FOR THE SETUP ONLY. This socket lives as long as the Telegram window is open,
        # and holding the session that long held a pooled connection idle INSIDE a transaction: Postgres
        # killed it at idle_in_transaction_session_timeout (60s), and the close below then raised out of
        # the handler ("Exception in ASGI application", ~7 an hour) -- one pool slot per open window.
        db = _release(db)
        await ws.send_text(json.dumps({"type": "state", **st}))

        async def pump():
            while True:
                await ws.send_text(json.dumps(await q.get()))

        async def drain():
            # A closed window must END this handler, or every tab ever opened stays subscribed.
            while True:
                m = await ws.receive()
                if m.get("type") == "websocket.disconnect":
                    return
        tasks = {asyncio.create_task(pump()), asyncio.create_task(drain())}
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.info("[tgc] socket ended: %s", type(e).__name__)
    finally:
        if q is not None and user is not None:
            manager().unsubscribe(user.id, q)
        _release(db)


# ---- voice and video calls --------------------------------------------------------------------------

class CallReq(BaseModel):
    action: str                     # start | accept | hangup | camera | state
    peer: int = 0
    video: bool = False


@router.post("/call")
async def call(req: CallReq, db: Session = Depends(get_db), user: User = Depends(member)):
    try:
        return {"ok": True, **await manager().call(db, user, req.action, req.peer, req.video)}
    except TGError as e:
        return _err(e)


@router.websocket("/call-media")
async def call_media(ws: WebSocket):
    """A call's audio and video (app/services/telegram_client/calls.py has the wire format). The session
    token arrives in the FIRST frame, as on /ws; after that the socket carries binary frames only."""
    from app.auth import decode_token
    await ws.accept()
    db = SessionLocal()
    hub = None
    try:
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=15))
        except Exception:
            await ws.close(code=4401)
            return
        payload = decode_token(str(first.get("token") or "")) if isinstance(first, dict) else None
        uid = payload.get("sub") if payload else None
        user = db.query(User).filter(User.id == int(uid)).first() if str(uid or "").isdigit() else None
        if user is None:
            await ws.close(code=4401)
            return
        try:
            await instance_membership.require_user(user)
            hub = await manager().call_hub(db, user)
        except Exception:
            await ws.close(code=4403)
            return
        db = _release(db)                 # the call can last an hour; the session is for the setup only
        hub.sockets.add(ws)
        await ws.send_text(json.dumps({"type": "call", "peer": hub.peer, "state": hub.state, "title": hub.title,
                                       "video": hub.video, "remote_video": hub.remote_video}))
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                return
            data = m.get("bytes")
            if data:
                await hub.from_browser(data)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.info("[tgc] call media socket ended: %s", type(e).__name__)
    finally:
        if hub is not None:
            hub.sockets.discard(ws)
        _release(db)
