from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, Query, UploadFile, File
from fastapi.responses import FileResponse, Response
from starlette.requests import Request
from pydantic import BaseModel
import asyncio
import re
import time
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional
from pathlib import Path
from urllib.parse import unquote
import json
import logging
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)

from app.database import get_db, SessionLocal
from app.models import User, Conversation, Message
from app.services import settings_store
from app.schemas import ConversationCreate, ConversationResponse, ConversationWithMessages, MessageResponse
from app.auth import get_current_user, get_user_from_websocket, get_ai_user
from app.services import chat_store, chat_history, artifact_store   # Phase 2: relay chat mirror + encrypted artifacts
from app.services.chat_service import ChatService
from app.services.command_service import CommandService
from app.services.storage_service import StorageService
from app.services.document_service import extract_pdf_text, extract_document_text, extract_image_text, merge_pdfs
from app.services.email_service import EmailService
from app.services.search_service import SearchService, is_safe_url
from app.services.proxy_image_cache import get as proxy_cache_get
from app.services.intent_service import IntentService

router = APIRouter(prefix="/api", tags=["chat"])


def _guess_image_content_type(b64: str) -> tuple:
    """Return (content_type, extension) from a base64 image payload."""
    if b64.startswith("/9j/"):
        return "image/jpeg", "jpg"
    if b64.startswith("R0lGOD"):
        return "image/gif", "gif"
    if b64.startswith("UklGR"):
        return "image/webp", "webp"
    return "image/png", "png"


def build_media_attachments(images, image_data, pdfs, pdf_data, documents, document_data, videos):
    """Decode webui upload arrays into (filename, bytes, content_type) tuples.

    Used by the `compress`/`convert` commands which operate on the raw file
    bytes rather than extracted text. Prefers the multi-attachment arrays and
    falls back to the single-value fields for backward compatibility.
    """
    import base64
    out = []

    def _add_image(b64, filename):
        try:
            ct, ext = _guess_image_content_type(b64)
            out.append((filename or f"image.{ext}", base64.b64decode(b64), ct))
        except Exception as e:
            logger.warning(f"[MEDIA] skipping bad image attachment: {e}")

    if images:
        for img in images:
            b64 = img.get("base64") if isinstance(img, dict) else img
            name = img.get("filename") if isinstance(img, dict) else None
            if b64:
                _add_image(b64, name)
    elif image_data:
        _add_image(image_data, None)

    if pdfs:
        for pdf in pdfs:
            b64 = pdf.get("base64") if isinstance(pdf, dict) else pdf
            name = pdf.get("filename", "document.pdf") if isinstance(pdf, dict) else "document.pdf"
            if b64:
                try:
                    out.append((name, base64.b64decode(b64), "application/pdf"))
                except Exception as e:
                    logger.warning(f"[MEDIA] skipping bad PDF attachment: {e}")
    elif pdf_data:
        try:
            out.append(("document.pdf", base64.b64decode(pdf_data), "application/pdf"))
        except Exception as e:
            logger.warning(f"[MEDIA] skipping bad PDF attachment: {e}")

    # Office docs / slide decks (docx/pptx/xlsx) — used by `flashcards` (text extraction).
    if documents:
        for doc in documents:
            b64 = doc.get("base64") if isinstance(doc, dict) else doc
            name = doc.get("filename", "document") if isinstance(doc, dict) else "document"
            ct = doc.get("type", "") if isinstance(doc, dict) else ""
            if b64:
                try:
                    out.append((name, base64.b64decode(b64), ct or "application/octet-stream"))
                except Exception as e:
                    logger.warning(f"[MEDIA] skipping bad document attachment: {e}")
    elif document_data:
        try:
            out.append(("document", base64.b64decode(document_data), "application/octet-stream"))
        except Exception as e:
            logger.warning(f"[MEDIA] skipping bad document attachment: {e}")

    if videos:
        for vid in videos:
            b64 = vid.get("base64") if isinstance(vid, dict) else vid
            name = vid.get("filename", "video.mp4") if isinstance(vid, dict) else "video.mp4"
            if b64:
                try:
                    out.append((name, base64.b64decode(b64), "video/mp4"))
                except Exception as e:
                    logger.warning(f"[MEDIA] skipping bad video attachment: {e}")

    return out or None


# Commands whose handlers consume the RAW uploaded file bytes (not extracted text). Shared by the
# chat WebSocket handler and the HTTP `/chat/send` fallback so both gate attachment-building the same.
# ----- Budget: "Add Bill with AI" — photo of a bill -> vendor/total/due -----
# The Budget view is client-side and encrypted, so it can't run OCR or call the model itself. This is
# the half the server CAN do, and it is deliberately the SAME code path as the `bill` chat command
# (CommandService._bill_command) rather than a second implementation — one OCR pipeline, one prompt,
# one set of guard rails (it refuses rather than guessing an amount). The client takes the parse,
# shows it for confirmation and writes the encrypted row itself.
@router.post("/budget/scan")
async def budget_scan(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="that image is too large (25 MB max)")
    cs = CommandService(db, user=current_user)
    res = await cs._bill_command("", attachments=[(file.filename or "bill.jpg", data, file.content_type or "")])
    # _bill_command returns {"type": "bill", vendor, amount, due} on a good read, or {"type": "text"}
    # carrying the reason it couldn't. Pass both through so the client can show the real message.
    return res

# Which commands get the upload's raw BYTES: CommandService.wants_attachments (the effect sets +
# the media tools, with aliases resolved). This used to be a hand-copied literal of all 99 names,
# which is why renaming one effect dropped the image from it.


async def _save_artifact_blossom(user_id: int, conv_id: int, data: bytes, ext: str,
                                 expires_days: int = 0) -> str | None:
    """Encrypt + store an artifact in Blossom on a FRESH DB session (returns its image_path, or None).
    A long render (60-90s music/video, a big media command) idles the request's held DB txn past Postgres
    idle_in_transaction_session_timeout, so THAT connection is dead by save time. We use a fresh session
    AND re-fetch the user on it (user.id, a PK, never expires) so nothing lazy-loads off the dead conn.
    `expires_days` > 0 sets a TTL — for transient agent workspace backups, so they don't fill storage."""
    from app.database import SessionLocal
    from app.models import User
    s = SessionLocal()
    try:
        u = s.get(User, user_id)
        if not u:
            return None
        return await artifact_store.save_bytes(s, u, conv_id, data, ext, expires_days=expires_days)
    except Exception as e:
        logger.warning("[CHAT] artifact Blossom save failed: %s", e)
        return None
    finally:
        s.close()


# Conversations with a command still running. A slow one (flashcards held the GPU for 222s) far
# outlives the websocket, so the chat looks dead and the natural reaction is to delete it — which
# purges the conversation ~a minute before its answer arrives, so the reply lands nowhere. Deleting
# now says so instead. Per-process (like the rest of the WS state) and TTL'd, so a crash mid-command
# can never wedge a conversation as undeletable.
_inflight: dict = {}
_INFLIGHT_TTL = 20 * 60

# Conversations the user DELETED — so a background agent that finishes mid-delete (its cancel raced the
# delivery) doesn't RESURRECT the chat the user just removed. TTL'd; the id is stamped in delete_conversation
# and checked in node_notify before it recreates a missing conversation.
_deleted_convs: dict = {}
_DELETED_TTL = 20 * 60


def _mark_deleted(conv_id: int) -> None:
    try:
        now = time.time()
        _deleted_convs[int(conv_id)] = now
        for k, ts in list(_deleted_convs.items()):
            if now - ts > _DELETED_TTL:
                _deleted_convs.pop(k, None)
    except Exception:
        pass


def _was_deleted(conv_id: int) -> bool:
    return int(conv_id) in _deleted_convs


def _mark_busy(conv_id: int) -> None:
    try:
        _inflight[int(conv_id)] = time.time()
    except Exception:
        pass


def _clear_busy(conv_id: int) -> None:
    _inflight.pop(int(conv_id), None)


def _busy_for(conv_id: int) -> float:
    """Seconds a command has been running on this conversation, or 0."""
    ts = _inflight.get(int(conv_id))
    if not ts:
        return 0.0
    age = time.time() - ts
    if age > _INFLIGHT_TTL:          # stale (process died mid-command) → not busy
        _inflight.pop(int(conv_id), None)
        return 0.0
    return age


def _artifact_url(rel: str, conv_id: int) -> str:
    """/api/files serve URL from a saved artifact path `<username>/chat/<conv>/<file>` — parses the
    username out of `rel` so we never touch the (possibly session-expired) request user."""
    from urllib.parse import quote as _q
    uname = str(rel).split("/", 1)[0]
    return f"/api/files/{_q(uname, safe='')}/{conv_id}/{_q(Path(rel).name)}"


async def normalize_command_result(db, user, conversation_id, result, storage_service):
    """Turn a CommandService result into (save_content, generated_image_path, live_result).

    The SINGLE source of truth for persisting a command's output, shared by the chat WebSocket handler
    and the HTTP `/chat/send` fallback so the two can't drift. `live_result` is what to push live (the
    'files' case is rewritten to text — raw bytes aren't JSON-serializable); `save_content` /
    `generated_image_path` are what's written to the Message row.
      - 'files'           → save each blob, append inline image/video/audio markdown or a download link
      - 'generated_image' → save the PNG (records last_image_prompts), return its path
      - 'flashcards'      → append the [[FC]]…[[/FC]] base64-JSON marker the client decodes on reload
      - anything else     → just its text content
    """
    from urllib.parse import quote as _q
    generated_image_path = None
    if result.get("type") == "files":
        _links = []
        for _f in result.get("files", []):
            _fbytes = _f.get("data")
            _fname = _f.get("filename", "file")
            _fct = (_f.get("content_type") or "").lower()
            if not _fbytes:
                continue
            try:
                if chat_store.enabled(db):
                    _ext = (Path(_fname).suffix.lstrip(".") or "bin")
                    _rel = await _save_artifact_blossom(user.id, conversation_id, _fbytes, _ext)   # fresh session
                    if not _rel:
                        raise RuntimeError("Blossom store failed")
                    _url = _artifact_url(_rel, conversation_id)
                else:
                    _rel = storage_service.save_file_bytes(user.username, conversation_id, _fbytes, _fname)
                    _url = f"/api/files/{_q(user.username, safe='')}/{conversation_id}/{_q(Path(_rel).name)}"
                # Encode the filename too — spaces/parens (e.g. "image (2).png") otherwise leave a raw ")"
                # that truncates the markdown link. Embed media INLINE so it shows/plays like geni; other
                # files (pdf/txt/…) keep the download link.
                if _fct.startswith("image/"):
                    _links.append(f"![{_fname}]({_url})")
                elif _fct.startswith("video/"):
                    _links.append(f"!video[{_fname}]({_url})")
                elif _fct.startswith("audio/"):
                    _links.append(f"!audio[{_fname}]({_url})")
                else:
                    _links.append(f"[⬇️ {_fname}]({_url})")
            except Exception as _save_err:
                logger.error(f"[CHAT] Failed to save output file {_fname}: {_save_err}")
                _links.append(f"❌ {_fname}: save failed")
        _content = result.get("content", "")
        if _links:
            _content += "\n\n" + "\n".join(_links)
        result = {"type": "text", "content": _content}

    if result.get("type") == "generated_image" and result.get("prompt"):
        manager.last_image_prompts[user.id] = result["prompt"]
        if result.get("image"):
            try:
                if chat_store.enabled(db):
                    import base64 as _b64g
                    # Extension follows the ACTUAL bytes: generated images are compressed to JPEG now
                    # (see _geni's `mime`), and a .png holding JPEG mislabels the blob's content-type
                    # for everything that serves it by extension.
                    _gext = "jpg" if result.get("mime") == "image/jpeg" else "png"
                    generated_image_path = await _save_artifact_blossom(   # fresh session (slow gen kills the held conn)
                        user.id, conversation_id, _b64g.b64decode(result["image"]), _gext)
                else:
                    generated_image_path = storage_service.save_image(user.username, conversation_id, result["image"], "generated")
            except Exception as save_err:
                logger.warning(f"Failed to save generated image to storage (non-fatal): {save_err}")
                generated_image_path = None

    save_content = result.get("content", "")
    # Persist generated music/video (branded MP4 / audio) like the `files` case, so it SURVIVES a dropped
    # WebSocket during the long (60-90s) render — a Thailand-latency WS dropout used to lose the song
    # entirely ("says generated song but no mp3") since the media was only pushed live, never saved — AND
    # so it shows on reload + in PosterChan AI files. Appended to save_content ONLY; the live result keeps
    # its base64 for an instant render, so the live message isn't doubled.
    _mkey = "video" if result.get("type") == "generated_video" else "audio" if result.get("type") == "generated_audio" else None
    if _mkey and result.get(_mkey):
        _mext = "mp4" if _mkey == "video" else (result.get("format") or "mp3").lower()
        try:
            import base64 as _b64m
            _mrel = await _save_artifact_blossom(user.id, conversation_id, _b64m.b64decode(result[_mkey]), _mext)
            if _mrel:
                # The label is not decoration: the persisted markdown is ALL the client gets when it
                # re-renders this message (the payload fields are long gone), so "does this MP4 have
                # an audio track" has to ride in it. `song` → the client offers Convert to MP3;
                # videogeni's silent clip stays `video` and doesn't.
                _mmd, _mlabel = ("!video", "song" if result.get("has_audio") else "video") \
                    if _mkey == "video" else ("!audio", "song")
                save_content = (save_content + f"\n\n{_mmd}[{_mlabel}]({_artifact_url(_mrel, conversation_id)})").strip()
            else:
                logger.warning(f"[CHAT] generated {_mkey} not persisted (Blossom store failed)")
        except Exception as _mv_err:
            logger.warning(f"[CHAT] failed to persist generated {_mkey} (non-fatal): {_mv_err}")
    if result.get("type") == "flashcards" and result.get("cards"):
        try:
            import base64 as _b64fc
            _fc_blob = _b64fc.b64encode(json.dumps(
                {"title": result.get("title"), "cards": result.get("cards")},
                ensure_ascii=False).encode("utf-8")).decode("ascii")
            save_content = (result.get("content", "") or "") + f"\n\n[[FC]]{_fc_blob}[[/FC]]"
        except Exception as _fc_err:
            logger.warning(f"[CHAT] flashcards persist marker failed (non-fatal): {_fc_err}")

    return save_content, generated_image_path, result


# REST Endpoints

@router.get("/conversations", response_model=List[ConversationResponse])
def list_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return db.query(Conversation).filter(
        Conversation.user_id == current_user.id,
        ~Conversation.title.startswith("📱")
    ).order_by(Conversation.updated_at.desc()).all()


@router.get("/node/state")
def node_state(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Read-only state for the Node Control panel: node NAMES (never the SSH targets) + THIS user's own
    jobs. Gated EXACTLY like the `node` command (node exec is unrestricted RCE) — a caller who isn't on
    the node_exec allowlist gets 403 and learns nothing about the fleet. All *actions* (run / kill / log)
    go through the already-gated chat command pipeline, so this endpoint mutates nothing."""
    from app.services import node_service
    _full = node_service.user_allowed(db, current_user)       # admin/allowlisted → host + remote nodes
    _sbx = node_service.sandbox_allowed(db, current_user)     # AI user + sandbox on → a Debian container
    if not _full and not _sbx:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Node access is not enabled for your account")
    # Per-user registry. Full-access users get the Nostr-only fleet (synthetic `local` + npub workers,
    # a self-mapped npub collapses to local) plus the sandbox if it's on; a sandbox-only user sees just
    # their container. Names only — the client hides the picker when there's a single target.
    if _full:
        names = list(node_service.all_nodes(db).keys())
        if _sbx:
            names.append("sandbox")
    else:
        names = ["sandbox"]
    jobs = [
        {"id": j.id, "node": j.node, "command": (j.command or "")[:120],
         "status": j.status, "exit_code": j.exit_code,
         "started_at": j.started_at, "finished_at": j.finished_at}
        for j in node_service.list_jobs(user_id=current_user.id, limit=20)   # owner-scoped; no output blob
    ]
    return {"enabled": True, "nodes": names, "jobs": jobs}


@router.post("/conversations", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def create_conversation(
    data: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_ai_user)   # starting a chat needs AI access
):
    conversation = Conversation(
        user_id=current_user.id,
        title=data.title or "New Chat"
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    # mirror the conversation index to the relay (the authoritative datastore)
    try:
        from app.services import chat_store
        import asyncio as _aio
        _aio.run(chat_store.mirror_conversation(db, current_user, conversation))
    except Exception as e:
        logger.warning(f"[chat] conversation mirror failed: {e}")
    return conversation


@router.get("/conversations/{conversation_id}", response_model=ConversationWithMessages)
async def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    def _img_url(image_path):
        if not image_path:
            return None
        from urllib.parse import quote
        name = str(image_path).rsplit("/", 1)[-1]
        return f"/api/files/{quote(current_user.username, safe='')}/{conversation_id}/{quote(name)}"

    # Phase 2: when the relay backs chats, load the (decrypted) message events instead of SQLite rows.
    try:
        if chat_store.enabled(db):
            rel = await chat_store.get_messages(db, current_user, conversation_id)
            # The transcript is written to the relay SYNCHRONOUSLY now (chat_history.append is awaited),
            # so it can no longer lag behind a SQL copy — the old count-comparison fallback to plaintext
            # `messages` rows is gone along with the rows themselves.
            if True:
                msgs = [{"id": i + 1, "role": m.get("role", ""), "content": m.get("content", ""),
                         "image_path": _img_url(m.get("image_path")),
                         "created_at": datetime.utcfromtimestamp(m.get("ts") or 0)}
                        for i, m in enumerate(rel)]
                return {"id": conversation.id, "title": conversation.title,
                        "created_at": conversation.created_at, "updated_at": conversation.updated_at,
                        "messages": msgs}
    except Exception as e:
        logger.warning("[CHAT] relay history load failed, falling back to DB: %s", e)
    # DB path: map each message's stored image_path to a served URL too (so the in-client AI
    # view can render generated/uploaded images on reload).
    # Relay read failed (above logs it). Return the conversation with no transcript rather than a
    # plaintext copy — there isn't one any more.
    return {"id": conversation.id, "title": conversation.title,
            "created_at": conversation.created_at, "updated_at": conversation.updated_at,
            "messages": []}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        # Already gone: deleting twice is not an error, and racing DELETEs used to 500 on the second
        # one (ObjectDeletedError) because the row vanished under the session.
        return {"ok": True, "already_deleted": True}

    # Cancel any background agent launched from this chat — otherwise deleting the chat leaves the run
    # churning and its result would RESURRECT the chat you just deleted. Cancel reaps its sandbox
    # container (the task's finally) and skips delivery.
    _mark_deleted(conversation_id)   # so a mid-delete agent delivery can't resurrect this chat (see node_notify)
    try:
        from app.services.command_service.system import cancel_agent_for_conv
        if cancel_agent_for_conv(conversation_id):
            logger.info("[node] cancelled background agent for deleted conv %s", conversation_id)
    except Exception as _e:
        logger.warning("[node] agent-cancel on delete failed for conv %s: %s", conversation_id, _e)

    # Refuse while a command is still running on this conversation. A slow one (flashcards took 222s)
    # outlives the websocket, the chat looks dead, and deleting it purges the conversation before the
    # answer lands — so the reply is lost with no trace. `?force=1` deletes anyway.
    _busy = _busy_for(conversation_id)
    if _busy and not force:
        raise HTTPException(status_code=409, detail={
            "error": "still working",
            "seconds": int(_busy),
            "message": f"I'm still working on this chat ({int(_busy)}s so far) — the answer would be "
                       f"lost if it's deleted now. Wait for it, or delete anyway.",
        })

    # Delete associated files (non-fatal: never let storage cleanup 500 the delete, or the chat
    # "never leaves" the list — attachment files may live on a remote node and error on cleanup).
    try:
        storage = StorageService(db)
        storage.delete_conversation_files(current_user.username, conversation_id)
    except Exception as e:
        logger.warning("[CHAT] file cleanup failed for conv %s (continuing): %s", conversation_id, e)

    # Phase 2: also purge the conversation's encrypted message events from the relay store.
    try:
        from app.services import chat_store, upload_store
        if chat_store.enabled(db):
            removed = await chat_store.delete_conversation(db, current_user, conversation_id)
            ups = await upload_store.delete_uploads(db, current_user, conversation_id)
            logger.info("[CHAT] purged %d message + %d upload event(s) for conv %s", removed, ups, conversation_id)
    except Exception as e:
        logger.warning("[CHAT] relay message purge failed: %s", e)

    db.delete(conversation)
    db.commit()
    return {"message": "Conversation deleted"}


@router.delete("/conversations")
def delete_all_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Delete all user files
    storage = StorageService(db)
    storage.delete_user_files(current_user.username)

    db.query(Conversation).filter(
        Conversation.user_id == current_user.id
    ).delete()
    db.commit()
    return {"message": "All conversations deleted"}


class CommandRequest(BaseModel):
    command: str

@router.post("/save-generated-image")
async def save_generated_image(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Save a generated image to user storage on demand."""
    try:
        data = await request.json()
        image_base64 = data.get("image")
        prompt = data.get("prompt", "")
        
        if not image_base64:
            raise HTTPException(status_code=400, detail="Image data is required")
        
        storage = StorageService(db)
        saved_path = storage.save_generated_image(current_user.username, image_base64, prompt)
        logger.info(f"Saved generated image to user storage: {saved_path}")
        
        # Generate viewable URL for the saved image
        from urllib.parse import quote
        encoded_username = quote(current_user.username, safe='')
        encoded_path = quote(saved_path, safe='')
        view_url = f"/api/files/view/{encoded_username}/{encoded_path}"
        
        return {"success": True, "path": saved_path, "view_url": view_url}
    except Exception as e:
        logger.error(f"Failed to save generated image: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _proxy_fetch(url_raw: str):
    """Fetch image from URL; returns (content, media_type) or raises HTTPException."""
    is_safe, err = is_safe_url(url_raw)
    if not is_safe:
        raise HTTPException(status_code=400, detail=err)
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        resp = await client.get(
            url_raw,
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36", "Accept": "image/*,*/*"},
        )
        resp.raise_for_status()
        content = resp.content
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        # SVG IS AN IMAGE, and it is XML — `"xml" in ctype` refused image/svg+xml, so an SVG result
        # from image search could never be proxied, saved or used in an effect. It came back as a
        # 502 "Upstream did not return an image" about a file that plainly was one. The guard is
        # meant to stop an HTML error page or a JSON body being served as a picture, so it now names
        # those rather than matching a substring that a real image type contains.
        if ctype and not ctype.startswith("image/") and (
                ctype.startswith("text/") or "json" in ctype or "xml" in ctype):
            raise HTTPException(status_code=502, detail="Upstream did not return an image")
        media_type = ctype if (ctype and ctype.startswith("image/")) else "image/png"
        return content, media_type


@router.get("/proxy-image/{thumb_id}")
async def proxy_image_by_id(
    thumb_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Proxy an image by short id (from image search). Keeps WebSocket payload small."""
    raw = proxy_cache_get(thumb_id, db)
    if not raw:
        raise HTTPException(status_code=404, detail="Unknown or expired image id")
    try:
        content, media_type = await _proxy_fetch(raw)
        return Response(content=content, media_type=media_type)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream error")
    except HTTPException:
        raise
    except Exception as e:
        logger.debug(f"Proxy image failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch image")


@router.get("/proxy-image")
async def proxy_image_get(
    url: str = Query(..., description="Image URL to proxy"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Proxy an image URL (GET; query length limit may truncate long URLs)."""
    raw = unquote(url)
    try:
        content, media_type = await _proxy_fetch(raw)
        return Response(content=content, media_type=media_type)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream error")
    except HTTPException:
        raise
    except Exception as e:
        logger.debug(f"Proxy image failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch image")


class ProxyImageBody(BaseModel):
    url: Optional[str] = None
    thumb_id: Optional[str] = None


@router.post("/proxy-image")
async def proxy_image_post(
    body: ProxyImageBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Proxy an image: send url (long URLs) or thumb_id (short id from image search). Auth via header/cookie."""
    raw = None
    if (body.thumb_id or "").strip():
        raw = proxy_cache_get((body.thumb_id or "").strip(), db)
        if not raw:
            raise HTTPException(status_code=404, detail="Unknown or expired image id")
    elif (body.url or "").strip():
        raw = (body.url or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="url or thumb_id required")
    try:
        content, media_type = await _proxy_fetch(raw)
        return Response(content=content, media_type=media_type)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream error")
    except HTTPException:
        raise
    except Exception as e:
        logger.debug(f"Proxy image failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch image")


@router.post("/merge-pdfs")
async def merge_pdfs_endpoint(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Merge multiple base64-encoded PDFs into one and return the merged PDF."""
    import base64 as _b64
    data = await request.json()
    pdfs = data.get("pdfs", [])  # list of {base64, filename}
    if len(pdfs) < 2:
        raise HTTPException(status_code=400, detail="At least 2 PDFs required for merging")
    try:
        pdf_bytes_list = [_b64.b64decode(p["base64"]) for p in pdfs if p.get("base64")]
        merged = merge_pdfs(pdf_bytes_list)
        if not merged:
            raise HTTPException(status_code=500, detail="PDF merge failed")
        return Response(
            content=merged,
            media_type="application/pdf",
            headers={"Content-Disposition": "attachment; filename=merged.pdf"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PDF merge endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save-mail-attachment")
async def save_mail_attachment(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Save a mail attachment to user storage on demand."""
    try:
        data = await request.json()
        attachment_data = data.get("data")  # base64 encoded
        filename = data.get("filename")
        
        if not attachment_data or not filename:
            raise HTTPException(status_code=400, detail="Attachment data and filename are required")
        
        # Decode base64 data
        import base64
        attachment_bytes = base64.b64decode(attachment_data)
        
        storage = StorageService(db)
        saved_path = storage.save_mail_attachment(current_user.username, attachment_bytes, filename)
        logger.info(f"Saved mail attachment to user storage: {saved_path}")
        
        # Generate URL to view the file
        from urllib.parse import quote
        encoded_username = quote(current_user.username, safe='')
        encoded_path = quote(saved_path, safe='')
        view_url = f"/api/files/view/{encoded_username}/{encoded_path}"
        
        return {"success": True, "path": saved_path, "view_url": view_url}
    except Exception as e:
        logger.error(f"Failed to save mail attachment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/command")
async def execute_command_endpoint(
    request: CommandRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Execute a command and return the result."""
    command_service = CommandService(db, user=current_user)
    command, arg = command_service.parse_command(request.command)
    if not command:
        return {"type": "text", "content": "Invalid command"}
    result = await command_service.execute_command(command, arg)
    return result


class ChatSendRequest(BaseModel):
    """Payload for the non-streaming HTTP fallback (mirrors the WS `message` frame)."""
    conversation_id: int
    content: str = ""
    images: list = []
    image_data: Optional[str] = None
    image_path: Optional[str] = None
    pdfs: list = []
    pdf_data: Optional[str] = None
    documents: list = []
    document_data: Optional[str] = None
    files: list = []
    videos: list = []


@router.post("/chat/send")
async def chat_send(
    req: ChatSendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """HTTP fallback for the chat WebSocket.

    Some networks/proxies (e.g. Cloudflare over HTTP/3) drop the WS upgrade, so the browser can never
    open `/api/ws/chat/{id}` and the user's message is silently never sent. This endpoint runs the SAME
    message handling NON-streamingly over plain HTTP and PERSISTS both messages exactly like the WS, so
    the client can render the reply (and a reload still shows it). Used by the client only when the
    socket fails to open. Covers commands/effects (incl. uploads) and basic LLM chat; advanced
    streaming niceties (intent tools, live token stream) stay WS-only.
    """
    user = current_user
    if not (getattr(user, "is_admin", False) or getattr(user, "can_ai", False)):
        raise HTTPException(status_code=403, detail="AI access not enabled")
    conversation_id = req.conversation_id
    conversation = db.query(Conversation).options(joinedload(Conversation.messages)).filter(
        Conversation.id == conversation_id, Conversation.user_id == user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    chat_service = ChatService(db, user=user)
    command_service = CommandService(db, user=user)
    storage_service = StorageService(db)

    content = (req.content or "").strip()
    images, image_data = req.images, req.image_data
    if images and not image_data:
        image_data = images[0].get("base64") if images else None
    documents, document_data = req.documents, req.document_data
    if documents and not document_data:
        document_data = documents[0].get("base64") if documents else None
    files, file_content = req.files, None
    if files:
        file_content = files[0].get("content") if files else None

    # --- persist the user message (save an uploaded image like the WS does) ---
    user_image_path = None
    if image_data:
        try:
            if chat_store.enabled(db):
                import base64 as _b64u2
                user_image_path = await artifact_store.save_bytes(
                    db, user, conversation_id, _b64u2.b64decode(image_data), "png")
            else:
                user_image_path = storage_service.save_image(user.username, conversation_id, image_data, "upload")
        except Exception as _e:
            logger.warning(f"[chat/send] user image save failed (non-fatal): {_e}")
            user_image_path = None
    try:
        # Transcript goes to the ENCRYPTED relay event ONLY — no plaintext row (see chat_history).
        prior = await chat_history.load(db, user, conversation_id)
        await chat_history.append(db, user, conversation_id, "user", content, image_path=user_image_path)
        first_msg = len(prior) == 0
        if first_msg:
            conversation.title = content[:50] + ("..." if len(content) > 50 else "")
        conversation.updated_at = datetime.utcnow()
        db.commit()
    except Exception as _umsg_err:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"could not save message: {_umsg_err}")
    if first_msg and chat_store.enabled(db):
        try:
            await chat_store.mirror_conversation(db, user, conversation)
        except Exception as e:
            logger.warning(f"[chat/send] conversation mirror failed: {e}")

    command, arg = command_service.parse_command(content)
    save_content, generated_image_path = "", None

    if command:
        media_attachments = None
        if CommandService.wants_attachments(command):
            media_attachments = build_media_attachments(
                images, image_data, req.pdfs, req.pdf_data, documents, document_data, req.videos)
        try:
            result = await command_service.execute_command(
                command, arg, manager.last_image_prompts.get(user.id), attachments=media_attachments)
        except Exception as cmd_err:
            logger.error(f"[chat/send] command failed: {cmd_err}", exc_info=True)
            db.rollback()
            result = {"type": "text", "content": f"Error: {cmd_err}"}

        # Persist the result via the SAME helper the WS path uses (files/generated_image/flashcards/text).
        save_content, generated_image_path, result = await normalize_command_result(
            db, user, conversation_id, result, storage_service)
    else:
        # Plain LLM chat (non-streaming). NOTE: intentionally a MINIMAL subset of the WS LLM path — it
        # omits the WS's URL-fetch / intent-detection / context-dependence heuristics on purpose (the
        # fallback's job is "the message still gets answered", not to reproduce every tuning knob).
        result = {"type": "text"}
        try:
            system_prompt = chat_service.system_prompt.replace("{{CURRENT_DATE}}", datetime.utcnow().strftime("%Y-%m-%d"))
            messages = [{"role": "system", "content": system_prompt}]
            last_role = "system"
            # Conversation memory comes from the encrypted transcript now, not SQL rows.
            messages += chat_history.for_llm(prior + [{"role": "user", "content": content}], content)
            save_content = await chat_service.chat(messages)
        except Exception as llm_err:
            logger.error(f"[chat/send] LLM failed: {llm_err}", exc_info=True)
            save_content = f"Error: {llm_err}"

    # --- persist the assistant message (retry once for a dropped idle DB conn, like the WS) ---
    saved = await chat_history.append(db, user, conversation_id, "assistant",
                                      save_content, image_path=generated_image_path)
    img_url = None
    if generated_image_path:
        from urllib.parse import quote as _q
        img_url = f"/api/files/{_q(user.username, safe='')}/{conversation_id}/{_q(Path(generated_image_path).name)}"
    return {"ok": bool(saved), "message": {
        "id": None, "role": "assistant",
        "content": save_content, "image_path": img_url, "type": result.get("type", "text")}}


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Transcript comes from the ENCRYPTED relay events (there are no plaintext rows any more).
    from urllib.parse import quote
    from datetime import datetime as _dt
    result = []
    for i, m in enumerate(await chat_history.load(db, current_user, conversation_id)):
        msg_dict = {
            "id": i + 1,
            "role": m.get("role", ""),
            "content": m.get("content", ""),
            "created_at": _dt.utcfromtimestamp(m.get("ts") or 0),
            "image_path": None
        }
        if m.get("image_path"):
            filename = Path(m["image_path"]).name
            msg_dict["image_path"] = f"/api/files/{quote(current_user.username, safe='')}/{conversation_id}/{quote(filename)}"
        result.append(msg_dict)
    return result


@router.get("/files/{username}/{conversation_id}/{filename}")
async def serve_file(
    username: str,
    conversation_id: int,
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Serve a stored file (image, document, etc.). Proxies to storage server if configured."""
    # Verify user owns this file (username must match)
    if current_user.username != username:
        raise HTTPException(status_code=403, detail="Access denied")

    # Verify conversation belongs to user
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Encrypted Blossom artifact (enc_<sha256>.<ext>): fetch ciphertext from Blossom + decrypt.
    _enc = re.match(r'^enc_([0-9a-fA-F]{64})\.(\w+)$', filename)
    if _enc:
        from app.services import artifact_store
        data = await artifact_store.read_bytes(db, current_user, _enc.group(1).lower())
        if data is None:
            raise HTTPException(status_code=404, detail="File not found")
        from mimetypes import guess_type as _gt
        ct = _gt("x." + _enc.group(2))[0] or "application/octet-stream"
        disp = "inline" if ct.startswith(("image/", "video/", "audio/")) else f'attachment; filename="{filename}"'
        return Response(content=data, media_type=ct, headers={"Content-Disposition": disp})

    from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base, ascii_safe_header_filename
    storage = StorageService(db)
    user_path = storage.get_conversation_path(current_user.username, conversation_id)

    # If the file exists locally, serve it directly (avoids 404 when saved locally but storage server is configured)
    try:
        _local_safe = _sanitize_path_component(filename)
        _local_path = user_path / _local_safe
        if _local_path.exists() and _local_path.is_file():
            from mimetypes import guess_type as _gt
            _ct, _ = _gt(str(_local_path))
            _ct = _ct or "application/octet-stream"
            # Serve media INLINE so the web UI plays/shows it in chat (like geni); FileResponse
            # supports HTTP Range so <video> can seek. Other files stay attachments (download).
            _inline = _ct.startswith(("image/", "video/", "audio/"))
            _disp = "inline" if _inline else f'attachment; filename="{_local_path.name}"'
            return FileResponse(str(_local_path), media_type=_ct,
                                headers={"Content-Disposition": _disp})
    except Exception:
        pass

    # Check if storage server is configured - proxy request if so
    storage_server_url = settings_store.get("storage_server_url")
    if storage_server_url:
        # Proxy to storage server
        from app.services.storage_proxy import proxy_storage_request
        return await proxy_storage_request(
            db=db,
            request=request,
            endpoint=f"/api/chat/files/{username}/{conversation_id}/{filename}",
            method="GET",
            stream=True
        )

    # On storage server: Use local filesystem
    
    # Sanitize filename
    try:
        safe_filename = _sanitize_path_component(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid filename: {str(e)}")
    
    file_path = user_path / safe_filename
    
    # Verify path is within user directory
    if not _validate_path_within_base(file_path, user_path):
        raise HTTPException(status_code=403, detail="Access denied: path outside user directory")
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    if file_path.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory, not a file")
    
    # Determine media type
    from mimetypes import guess_type
    content_type, _ = guess_type(str(file_path))
    if not content_type:
        suffix = Path(filename).suffix.lower()
        media_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".pdf": "application/pdf",
            ".txt": "text/plain",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        content_type = media_types.get(suffix, "application/octet-stream")
    
    # Read file
    def _read_file_sync():
        with open(file_path, 'rb') as f:
            return f.read()
    
    file_data = await asyncio.to_thread(_read_file_sync)
    
    # Return file response (ASCII-safe filename for Content-Disposition header). NOTE: Response is
    # imported at module top; do NOT re-import here (a local import makes Response function-local and
    # breaks the earlier encrypted-blob branch with UnboundLocalError).
    safe_name = ascii_safe_header_filename(filename)
    return Response(
        content=file_data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"'
        }
    )


@router.post("/chat/email-response")
def email_response(
    data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Email an AI response to the user's notification email"""
    content = data.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="No content to email")

    if not current_user.notification_email:
        raise HTTPException(status_code=400, detail="No notification email configured. Please set one in Settings.")

    email_service = EmailService(db)
    if not email_service.smtp_enabled:
        raise HTTPException(status_code=400, detail="Email is not configured on this server")

    success, message = email_service.send_chat_response(
        to_email=current_user.notification_email,
        username=current_user.username,
        content=content
    )

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {message}")

    return {"message": "Email sent successfully"}


@router.get("/news-sources")
def get_news_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get configured news sources for the News modal"""
    setting = settings_store.get("news_sources")

    # Default news sources if not configured
    default_sources = """drudgereport.com|Drudge Report
usatoday.com|USA Today
msn.com|MSN
cnn.com|CNN
foxnews.com|Fox News"""

    raw = setting if setting else default_sources

    sources = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if "|" in line:
            url, name = line.split("|", 1)
            sources.append({"url": url.strip(), "name": name.strip()})
        elif line:
            # Just a URL without a name
            sources.append({"url": line, "name": line})

    return {"sources": sources}


# WebSocket for real-time chat

class ConnectionManager:
    # Keyed by the globally-unique conn_id (one per live socket), NOT per user — so multiple conversations
    # for the same user each keep their own live socket. The old per-user registry let a second open chat
    # (e.g. the effects studio opens its OWN conversation) steal the single slot, so the first chat's reply
    # was queued instead of delivered live: the "effect result never updates until refresh" bug.
    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}   # conn_id -> socket
        self.user_conns: dict[int, set] = {}                 # user_id -> {conn_id, …}  (user-level broadcast, e.g. reminders)
        self.connection_ids: dict[tuple, int] = {}           # (user_id, conv_id) -> CURRENT conn_id (staleness + reconnect continuity)
        self.last_image_prompts: dict[int, str] = {}         # per-user (img2img convenience)
        self.stop_flags: dict[tuple, bool] = {}              # (user_id, conv_id) -> stop
        self.pending_results: dict[tuple, list] = {}         # (user_id, conv_id) -> list of pending results
        self._next_conn_id = 0
        self._conn_lock = asyncio.Lock()  # Protect connection ID increment

    async def connect(self, user_id: int, conversation_id: int, websocket: WebSocket) -> int:
        if websocket.client_state.name != "CONNECTED":
            await websocket.accept()
        key = (user_id, conversation_id)
        async with self._conn_lock:
            # Reconnect to the SAME conversation reuses its conn_id so an in-flight generation survives the
            # socket swap (it checks conn_id, not the socket). A first connect gets a fresh id.
            conn_id = self.connection_ids.get(key)
            if conn_id is None:
                self._next_conn_id += 1
                conn_id = self._next_conn_id
                self.connection_ids[key] = conn_id
            self.active_connections[conn_id] = websocket
            self.user_conns.setdefault(user_id, set()).add(conn_id)
        self.stop_flags[key] = False
        return conn_id

    def disconnect(self, user_id: int, conversation_id: int, conn_id: int = None, websocket: WebSocket = None):
        key = (user_id, conversation_id)
        # Only drop the socket if it's still the one we hold — a fast reconnect may have already replaced it.
        if conn_id is not None and (websocket is None or self.active_connections.get(conn_id) is websocket):
            self.active_connections.pop(conn_id, None)
            conns = self.user_conns.get(user_id)
            if conns is not None:
                conns.discard(conn_id)
                if not conns:
                    self.user_conns.pop(user_id, None)
        self.stop_flags.pop(key, None)
        # Keep connection_ids[key] so a reconnect to this conversation keeps its conn_id (in-flight continuity).

    def should_stop(self, user_id: int, conn_id: int = None, conversation_id: int = None) -> bool:
        key = (user_id, conversation_id)
        # Stop if flag is set OR if this conn_id is no longer the current one for its conversation (superseded).
        if self.stop_flags.get(key, False):
            return True
        if conn_id is not None and self.connection_ids.get(key) != conn_id:
            return True
        return False

    def set_stop(self, user_id: int, value: bool, conversation_id: int = None):
        self.stop_flags[(user_id, conversation_id)] = value

    def queue_result(self, user_id: int, conversation_id: int, data: dict):
        """Queue a result for later delivery when user reconnects to this conversation"""
        key = (user_id, conversation_id)
        if key not in self.pending_results:
            self.pending_results[key] = []
        self.pending_results[key].append(data)
        logger.debug(f"Saved pending result for user {user_id}, conv {conversation_id}")

    def get_pending_results(self, user_id: int, conversation_id: int) -> list:
        """Get and clear pending results for a conversation"""
        key = (user_id, conversation_id)
        results = self.pending_results.pop(key, [])
        if results:
            logger.debug(f"Delivering {len(results)} pending result(s) to user {user_id}, conv {conversation_id}")
        return results

    async def send_json(self, user_id: int, data: dict, conn_id: int = None, conversation_id: int = None):
        # A specific connection (a generation delivering to its own socket): if this conn_id is no longer
        # current for its conversation, it's stale → queue the response for the reconnect instead of sending.
        if conn_id is not None and conversation_id is not None and self.connection_ids.get((user_id, conversation_id)) != conn_id:
            if data.get("type") == "response":
                self.queue_result(user_id, conversation_id, data)
            return
        if conn_id is not None:
            ws = self.active_connections.get(conn_id)
            targets = [ws] if ws else []
        else:
            # No conn_id → a user-level broadcast (e.g. a fired reminder): deliver to every open socket the user has.
            targets = [self.active_connections[c] for c in list(self.user_conns.get(user_id, ()))
                       if c in self.active_connections]
        sent = False
        for ws in targets:
            try:
                await ws.send_json(data)
                sent = True
            except Exception:
                pass  # Connection may be closed
        if not sent and conn_id is not None and conversation_id is not None and data.get("type") == "response":
            self.queue_result(user_id, conversation_id, data)  # nothing live got it → queue for reconnect


manager = ConnectionManager()

# Router with no prefix so /ws/chat/{id} works (some clients connect without /api)
ws_only_router = APIRouter(tags=["chat"])


@router.websocket("/ws/chat/{conversation_id}")
@ws_only_router.websocket("/ws/chat/{conversation_id}")
async def websocket_chat(websocket: WebSocket, conversation_id: int):
    """Chat WebSocket. Accept immediately so we never return HTTP 403 (avoids proxy/WAF issues)."""
    conn_id = None
    user = None
    db = None
    logger.info("WebSocket /ws/chat/%s connection attempt", conversation_id)
    await websocket.accept()
    try:
        db = SessionLocal()
        user = await get_user_from_websocket(websocket, db)
        if not user:
            await websocket.send_json({"type": "error", "message": "Please log in again"})
            await websocket.close(code=4001)
            return
        # AI gate: admins always; everyone else needs the admin-granted can_ai flag (Nostr-signup
        # users start gated and request access). Enforced here so the UI gate isn't the only check.
        if not (getattr(user, "is_admin", False) or getattr(user, "can_ai", False)):
            await websocket.send_json({"type": "error", "message": "AI access not enabled — request access and an admin will approve."})
            await websocket.close(code=4003)
            return

        # Verify conversation belongs to user (eagerly load messages to avoid N+1 queries)
        conversation = db.query(Conversation).options(
            joinedload(Conversation.messages)
        ).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id
        ).first()
        if not conversation:
            await websocket.send_json({"type": "error", "message": "Conversation not found"})
            await websocket.close(code=4004)
            return

        # Use manager.connect() which handles stopping old streams and returns connection ID
        try:
            conn_id = await manager.connect(user.id, conversation_id, websocket)
        except Exception as connect_err:
            logger.error(f"Failed to connect websocket: {connect_err}", exc_info=True)
            try:
                await websocket.send_json({"type": "error", "message": "Failed to establish connection"})
                await websocket.close(code=4000)
            except Exception:
                pass
            return

        # Check for and deliver any pending results from previous sessions. Tag them `pending` so a
        # client that reloads the conversation from the DB on connect (the web client always does)
        # can skip the replay instead of rendering the same reply twice — the result was persisted
        # before it was ever queued, so the DB reload already shows it.
        pending = manager.get_pending_results(user.id, conversation_id)
        for pending_data in pending:
            try:
                await websocket.send_json({**pending_data, "pending": True})
            except Exception:
                pass

        chat_service = ChatService(db, user=user)
        command_service = CommandService(db, user=user)
        storage_service = StorageService(db)
        search_service = SearchService(db)
        intent_service = IntentService(db, user=user)

        try:
            while True:
                try:
                    # Check if websocket is still connected before receiving
                    if websocket.client_state.name != "CONNECTED":
                        logger.debug("WebSocket is not connected, breaking loop")
                        break
                    # Use receive_text to get better error info, then parse JSON
                    raw_text = await websocket.receive_text()
                    logger.debug(f"Received raw text length: {len(raw_text)}")
                    data = json.loads(raw_text)
                except json.JSONDecodeError as json_err:
                    logger.debug(f"JSON parse failed: {json_err}")
                    continue
                except Exception as recv_err:
                    logger.debug(f"Failed to receive: {type(recv_err).__name__}: {recv_err}")
                    raise
                logger.debug(f"Received: type={data.get('type')}, content={data.get('content', '')[:50] if data.get('content') else ''}, has_image={data.get('image_data') is not None}")

                if data.get("type") == "stop":
                    manager.set_stop(user.id, True, conversation_id)
                    continue

                if data.get("type") == "message":
                    manager.set_stop(user.id, False, conversation_id)  # Reset for new message
                    content = data.get("content", "").strip()
                    image_data = data.get("image_data")  # base64 image (single, for backward compat)
                    images = data.get("images", [])  # Array of {base64, filename}
                    image_path = data.get("image_path")  # path to stored image (for editing)
                    file_content = data.get("file_content")  # text file content (single, for backward compat)
                    files = data.get("files", [])  # Array of {content, filename}
                    pdf_data = data.get("pdf_data")  # base64 PDF (single, for backward compat)
                    pdfs = data.get("pdfs", [])  # Array of {base64, filename}
                    document_data = data.get("document_data")  # base64 Office document (single, for backward compat)
                    documents = data.get("documents", [])  # Array of {base64, filename, type}
                    videos = data.get("videos", [])  # Array of {base64, filename} (for compress)

                    # If arrays are provided, use them; otherwise fall back to single values for backward compat
                    if images and not image_data:
                        image_data = images[0].get("base64") if images else None
                    if documents and not document_data:
                        document_data = documents[0].get("base64") if documents else None
                    if files and not file_content:
                        file_content = files[0].get("content") if files else None

                    # Phase 2: persist every AI upload to the built-in Blossom server, ENCRYPTED to
                    # the user's storage key (background, additive). The plaintext arrays below are
                    # still used in-memory by vision/commands, so all upload features keep working.
                    if chat_store.enabled(db):
                        try:
                            import base64 as _b64u
                            _ups = []
                            for _img in (images or ([{"base64": image_data, "filename": "image"}] if image_data else [])):
                                if _img.get("base64"):
                                    _ups.append((_img.get("filename") or "image", _b64u.b64decode(_img["base64"]), "image/*"))
                            for _p in (pdfs or ([{"base64": pdf_data, "filename": "document.pdf"}] if pdf_data else [])):
                                if _p.get("base64"):
                                    _ups.append((_p.get("filename") or "document.pdf", _b64u.b64decode(_p["base64"]), "application/pdf"))
                            for _d in (documents or []):
                                if _d.get("base64"):
                                    _ups.append((_d.get("filename") or "document", _b64u.b64decode(_d["base64"]), _d.get("type") or "application/octet-stream"))
                            for _f in (files or []):
                                if _f.get("content") is not None:
                                    _ups.append((_f.get("filename") or "file.txt", str(_f["content"]).encode("utf-8", "replace"), "text/plain"))
                            if _ups:
                                from app.services import upload_store
                                async def _persist_uploads(items, uid=user.id, cid=conversation_id):
                                    _db = SessionLocal()
                                    try:
                                        _u = _db.query(User).filter(User.id == uid).first()
                                        for name, b, mime in items:
                                            await upload_store.store_encrypted(_db, _u, cid, name, b, mime)
                                    finally:
                                        _db.close()
                                asyncio.create_task(_persist_uploads(_ups))
                        except Exception as _e:
                            logger.warning("[CHAT] encrypted upload persist failed: %s", _e)
                    # PDFs: detect merge/combine intent early — do it server-side, independent of analysis
                    import base64 as _b64
                    _is_merge_intent = (
                        len(pdfs) >= 2 and
                        bool(re.search(r'\b(merge|combine|join|concatenate|concat)\b', content.lower() if content else ""))
                    )
                    if _is_merge_intent:
                        try:
                            _pdf_bytes_list = [_b64.b64decode(p["base64"]) for p in pdfs if p.get("base64")]
                            _merged = merge_pdfs(_pdf_bytes_list)
                            if _merged:
                                _pdf_names = [p.get("filename", f"file{i}.pdf") for i, p in enumerate(pdfs)]
                                try:
                                    from urllib.parse import quote
                                    if chat_store.enabled(db):
                                        _saved_rel = await artifact_store.save_bytes(db, user, conversation_id, _merged, "pdf")
                                    else:
                                        _saved_rel = storage_service.save_file_bytes(user.username, conversation_id, _merged, "merged.pdf")
                                    _saved_filename = Path(_saved_rel).name
                                    _dl_url = f"/api/files/{quote(user.username, safe='')}/{conversation_id}/{quote(_saved_filename)}"
                                    _reply = f"✅ Merged {len(_pdf_bytes_list)} PDFs: {', '.join(_pdf_names)}\n\n[⬇️ Download merged.pdf]({_dl_url})"
                                except Exception as _save_err:
                                    logger.error(f"[CHAT] Failed to save merged PDF: {_save_err}")
                                    _reply = f"✅ Merged {len(_pdf_bytes_list)} PDFs but could not save to storage: {_save_err}"
                                # Save messages and stream reply
                                await chat_history.append(db, user, conversation_id, "user", content or "Merge PDFs")
                                await chat_history.append(db, user, conversation_id, "assistant", _reply)
                                await websocket.send_json({"type": "stream", "content": _reply})
                                await websocket.send_json({"type": "stream_end"})
                                continue
                            else:
                                await websocket.send_json({"type": "stream", "content": "❌ PDF merge failed — could not process the uploaded files."})
                                await websocket.send_json({"type": "stream_end"})
                                continue
                        except Exception as _merge_top_err:
                            logger.error(f"[CHAT] PDF merge error: {_merge_top_err}", exc_info=True)
                            await websocket.send_json({"type": "stream", "content": f"❌ PDF merge error: {_merge_top_err}"})
                            await websocket.send_json({"type": "stream_end"})
                            continue
                    # END THE TRANSACTION BEFORE THE SLOW ATTACHMENT WORK.
                    #
                    # Everything below — PDF merge/extract, document extract, and above all
                    # artifact_store.save_bytes (encrypt + upload to Blossom) — can run for well over
                    # a minute on a video. Holding this request's transaction open across it hits
                    # Postgres' idle_in_transaction_session_timeout (60s, set in database.py), which
                    # KILLS the connection; the next statement then dies with "server closed the
                    # connection unexpectedly" and takes the whole websocket down. Seen on
                    # `extractaudio` with a video attached: the conversation-exists guard right after
                    # the upload was the statement that hit the dead socket, so the command failed
                    # with no result and no error shown to the user.
                    #
                    # pool_pre_ping does NOT cover this: it validates a connection as it is checked
                    # OUT of the pool, and this one is already held by the session for the whole
                    # upload. Committing here releases it, so the next query checks out a fresh,
                    # pre-pinged connection instead. Nothing is pending but reads at this point, so
                    # the commit itself writes nothing.
                    try:
                        db.commit()
                    except Exception as _txn_err:
                        logger.warning("[CHAT] could not release the DB transaction before upload: %s", _txn_err)
                        try:
                            db.rollback()
                        except Exception:
                            pass

                    # PDFs: if multiple provided (not merge intent), merge for unified text extraction
                    if pdfs and len(pdfs) > 1:
                        _pdf_bytes_list = [_b64.b64decode(p["base64"]) for p in pdfs if p.get("base64")]
                        _merged = merge_pdfs(_pdf_bytes_list)
                        if _merged:
                            pdf_data = _b64.b64encode(_merged).decode("utf-8")
                            logger.info(f"[CHAT] Merged {len(_pdf_bytes_list)} PDFs into {len(_merged)} bytes")
                        else:
                            pdf_data = pdfs[0].get("base64") if pdfs else None
                    elif pdfs and not pdf_data:
                        pdf_data = pdfs[0].get("base64") if pdfs else None

                    # If image_path provided but no image_data, load from disk
                    if image_path and not image_data:
                        try:
                            # Log the image_path to debug emoji issues
                            logger.info(f"[CHAT] Loading image from path: {repr(image_path)} (length={len(image_path) if image_path else 0})")
                            loaded_image = storage_service.load_image_as_base64(image_path)
                            if loaded_image:
                                image_data = loaded_image
                                logger.debug(f"Loaded image from path: {image_path}")
                        except Exception as e:
                            logger.warning(f"[CHAT] Failed to load image from path {repr(image_path)}: {e}", exc_info=True)

                    # Extract text from PDF if provided
                    if pdf_data:
                        extracted = extract_pdf_text(pdf_data)
                        if extracted:
                            file_content = f"[PDF Document]\n\n{extracted}"

                    # Extract text from Office document if provided
                    if document_data:
                        extracted = extract_document_text(document_data)
                        if extracted:
                            file_content = f"[Office Document]\n\n{extracted}"

                    if not content and not file_content and not image_data:
                        continue

                    # Save the uploaded image — encrypted in Blossom (relay backend) or to disk.
                    user_image_path = None
                    if image_data:
                        if chat_store.enabled(db):
                            import base64 as _b64u2
                            try:
                                user_image_path = await artifact_store.save_bytes(db, user, conversation_id, _b64u2.b64decode(image_data), "png")
                            except Exception:
                                user_image_path = None
                        else:
                            user_image_path = storage_service.save_image(user.username, conversation_id, image_data, "upload")
                    # NOTE: the extracted text of an uploaded document used to be written to local
                    # disk here (storage_service.save_file) — unconditionally, unlike every other
                    # write in this file, which goes to encrypted Blossom when chat_store is on. It
                    # was also WRITE-ONLY: the return value was discarded and nothing ever read those
                    # files back, so it produced nothing but a plaintext copy of the document on the
                    # server. The upload itself is already stored encrypted (upload_store), so the
                    # write is gone rather than ported.

                    # The attachment upload above can take seconds — long enough for the user to
                    # delete this conversation meanwhile. Inserting the message now would violate the
                    # messages→conversations FK, crash the socket, and leave the shared DB session in an
                    # aborted state — which then 500s the delete/list calls (the bug where deleting a
                    # chat WITH an attachment "never leaves" the list). Re-check, then guard the insert.
                    if not db.query(Conversation.id).filter(Conversation.id == conversation_id).first():
                        try:
                            db.rollback()
                        except Exception:
                            pass
                        logger.info(f"[CHAT] conversation {conversation_id} deleted mid-request; dropping message")
                        await manager.send_json(user.id, {"type": "stream_end"}, conn_id)
                        continue

                    # Save the user message as an ENCRYPTED relay event (no plaintext SQL row).
                    _prior = []          # defined up-front: the LLM context below reads it, and an
                    try:                 # exception in this block must not turn that into a NameError
                        _prior = await chat_history.load(db, user, conversation_id)
                        await chat_history.append(db, user, conversation_id, "user", content,
                                                  image_path=user_image_path)

                        # Update conversation title if it's the first message
                        first_msg = len(_prior) == 0
                        if first_msg:
                            conversation.title = content[:50] + ("..." if len(content) > 50 else "")

                        conversation.updated_at = datetime.utcnow()
                        db.commit()
                    except Exception as _umsg_err:
                        try:
                            db.rollback()
                        except Exception:
                            pass
                        logger.warning(f"[CHAT] user message save aborted (conversation gone?): {_umsg_err}")
                        await manager.send_json(user.id, {"type": "stream_end"}, conn_id)
                        continue
                    # mirror the conversation index (title/timestamp) to the relay on first message
                    if first_msg and chat_store.enabled(db):
                        try:
                            await chat_store.mirror_conversation(db, user, conversation)
                        except Exception as e:
                            logger.warning(f"[chat] conversation mirror failed: {e}")

                    # Check for commands
                    command, arg = command_service.parse_command(content)

                    # An uploaded image/PDF + a "translate" request (even in natural
                    # language, e.g. "translate this to spanish") → run the translate
                    # command so it OCRs and translates the FULL text. Otherwise it falls
                    # to the chat path, whose summary-style prompt makes the model emit a
                    # preamble and stop after a couple hundred characters.
                    if (not command and content and (image_data or images or pdfs or pdf_data)
                            and re.search(r'\btranslate', content, re.IGNORECASE)):
                        command = "translate"
                        # Capture the language after "to" (1-2 words at the end, so
                        # "brazilian portuguese" / "simplified chinese" survive).
                        _lang_m = re.search(r'\bto\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\s*$', content, re.IGNORECASE)
                        arg = _lang_m.group(1) if _lang_m else ""

                    # Check for YouTube URLs (auto-summarize)
                    if not command:
                        youtube_result = await command_service.check_youtube_url(content)
                        if youtube_result:
                            # Save and send YouTube summary
                            await chat_history.append(db, user, conversation_id, "assistant",
                                                      youtube_result.get("content", ""))
                            await manager.send_json(user.id, {
                                "type": "response",
                                "data": youtube_result
                            }, conn_id, conversation_id)
                            await manager.send_json(user.id, {"type": "stream_end"}, conn_id)
                            continue

                    if command:
                        # Execute command with stop check
                        try:
                            # Check if already stopped before starting
                            if manager.should_stop(user.id, conn_id, conversation_id):
                                logger.debug("Command cancelled before start")
                                continue

                            logger.debug(f"Executing command: {command} with arg: {arg[:50] if arg else ''}, has_image: {image_data is not None}")
                            last_prompt = manager.last_image_prompts.get(user.id)

                            # Create stop check function for long-running commands
                            def should_stop_command():
                                return manager.should_stop(user.id, conn_id, conversation_id)

                            # Prepare attachments for mail command - support multiple attachments
                            mail_attachments = None
                            if command == "mail":
                                import base64
                                mail_attachments = []

                                # Handle multiple image attachments
                                if images:
                                    for img in images:
                                        try:
                                            img_base64 = img.get("base64") or img  # Support both object and string
                                            if isinstance(img, dict):
                                                img_base64 = img.get("base64")
                                            else:
                                                img_base64 = img
                                            img_bytes = base64.b64decode(img_base64)
                                            filename = img.get("filename", "image") if isinstance(img, dict) else "image"
                                            content_type = "image/png"
                                            if img_base64.startswith("/9j/"):
                                                content_type = "image/jpeg"
                                                if not filename.endswith(('.jpg', '.jpeg')):
                                                    filename = f"{filename}.jpg" if '.' not in filename else filename.rsplit('.', 1)[0] + '.jpg'
                                            elif img_base64.startswith("R0lGOD"):
                                                content_type = "image/gif"
                                                if not filename.endswith('.gif'):
                                                    filename = f"{filename}.gif" if '.' not in filename else filename.rsplit('.', 1)[0] + '.gif'
                                            else:
                                                if not filename.endswith('.png'):
                                                    filename = f"{filename}.png" if '.' not in filename else filename.rsplit('.', 1)[0] + '.png'
                                            mail_attachments.append((filename, img_bytes, content_type))
                                        except Exception as att_err:
                                            logger.warning(f"Failed to process image attachment: {att_err}")
                                elif image_data:  # Backward compat: single image
                                    try:
                                        img_bytes = base64.b64decode(image_data)
                                        content_type = "image/png"
                                        if image_data.startswith("/9j/"):
                                            content_type = "image/jpeg"
                                        elif image_data.startswith("R0lGOD"):
                                            content_type = "image/gif"
                                        ext = content_type.split("/")[1]
                                        mail_attachments.append((f"image.{ext}", img_bytes, content_type))
                                    except Exception as att_err:
                                        logger.warning(f"Failed to process image attachment: {att_err}")

                                # Handle multiple PDF attachments
                                if pdfs:
                                    for pdf in pdfs:
                                        try:
                                            pdf_base64 = pdf.get("base64") if isinstance(pdf, dict) else pdf
                                            pdf_bytes = base64.b64decode(pdf_base64)
                                            filename = pdf.get("filename", "document.pdf") if isinstance(pdf, dict) else "document.pdf"
                                            mail_attachments.append((filename, pdf_bytes, "application/pdf"))
                                        except Exception as att_err:
                                            logger.warning(f"Failed to process PDF attachment: {att_err}")
                                elif pdf_data:  # Backward compat: single PDF
                                    try:
                                        pdf_bytes = base64.b64decode(pdf_data)
                                        mail_attachments.append(("document.pdf", pdf_bytes, "application/pdf"))
                                    except Exception as att_err:
                                        logger.warning(f"Failed to process PDF attachment: {att_err}")

                                # Handle multiple Office document attachments
                                if documents:
                                    for doc in documents:
                                        try:
                                            doc_base64 = doc.get("base64") if isinstance(doc, dict) else doc
                                            doc_bytes = base64.b64decode(doc_base64)
                                            doc_type = doc.get("type", "docx") if isinstance(doc, dict) else "docx"
                                            filename = doc.get("filename", "document") if isinstance(doc, dict) else "document"
                                            # Try to guess type from content
                                            content_type = "application/octet-stream"
                                            if doc_bytes[:4] == b'PK\x03\x04':  # ZIP-based (docx, xlsx, pptx)
                                                if b'word/' in doc_bytes[:2000]:
                                                    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                                                    if not filename.endswith('.docx'):
                                                        filename = f"{filename}.docx" if '.' not in filename else filename.rsplit('.', 1)[0] + '.docx'
                                                elif b'xl/' in doc_bytes[:2000]:
                                                    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                                    if not filename.endswith('.xlsx'):
                                                        filename = f"{filename}.xlsx" if '.' not in filename else filename.rsplit('.', 1)[0] + '.xlsx'
                                                else:
                                                    if not filename.endswith('.pptx'):
                                                        filename = f"{filename}.pptx" if '.' not in filename else filename.rsplit('.', 1)[0] + '.pptx'
                                            mail_attachments.append((filename, doc_bytes, content_type))
                                        except Exception as att_err:
                                            logger.warning(f"Failed to process document attachment: {att_err}")
                                elif document_data:  # Backward compat: single document
                                    try:
                                        doc_bytes = base64.b64decode(document_data)
                                        # Try to guess type from content
                                        content_type = "application/octet-stream"
                                        filename = "document"
                                        if doc_bytes[:4] == b'PK\x03\x04':  # ZIP-based (docx, xlsx, pptx)
                                            if b'word/' in doc_bytes[:2000]:
                                                content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                                                filename = "document.docx"
                                            elif b'xl/' in doc_bytes[:2000]:
                                                content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                                filename = "spreadsheet.xlsx"
                                            else:
                                                filename = "document.docx"
                                        mail_attachments.append((filename, doc_bytes, content_type))
                                    except Exception as att_err:
                                        logger.warning(f"Failed to process document attachment: {att_err}")

                                # Handle multiple text file attachments (only if no PDF/document was sent)
                                if files and not pdfs and not documents:
                                    for file_item in files:
                                        try:
                                            file_content_item = file_item.get("content") if isinstance(file_item, dict) else file_item
                                            filename = file_item.get("filename", "attachment.txt") if isinstance(file_item, dict) else "attachment.txt"
                                            # Only attach raw text files, not extracted content
                                            if not file_content_item.startswith("[PDF Document]") and not file_content_item.startswith("[Office Document]"):
                                                if len(file_content_item) < 50000:  # Reasonable file size
                                                    mail_attachments.append((filename, file_content_item.encode("utf-8"), "text/plain"))
                                        except Exception as att_err:
                                            logger.warning(f"Failed to process text file attachment: {att_err}")
                                elif file_content and not pdf_data and not document_data:  # Backward compat: single file
                                    # Only attach raw text files, not extracted content
                                    if not file_content.startswith("[PDF Document]") and not file_content.startswith("[Office Document]"):
                                        if len(file_content) < 50000:  # Reasonable file size
                                            mail_attachments.append(("attachment.txt", file_content.encode("utf-8"), "text/plain"))

                                if not mail_attachments:
                                    mail_attachments = None

                            # compress/clip/convert/translate operate on raw file bytes
                            # (translate OCRs an uploaded image/PDF and translates the text)
                            media_attachments = None
                            if CommandService.wants_attachments(command):
                                media_attachments = build_media_attachments(
                                    images, image_data, pdfs, pdf_data,
                                    documents, document_data, videos
                                )

                            # For `node`, long-running jobs finish after this request
                            # returns. Deliver their output back into THIS conversation
                            # (so web-UI results don't leak to Telegram). The callback runs
                            # later, so it must use its own DB session.
                            node_notify = None
                            if command in ("node", "logs"):
                                _uid, _conn, _conv, _uname, _cmd = user.id, conn_id, conversation_id, user.username, command

                                async def node_notify(job):
                                    from urllib.parse import quote as _q
                                    from app.database import SessionLocal
                                    from app.models import Message as _Msg, Conversation as _Conv
                                    from app.services.node_service import tail as _tail, INLINE_LIMIT as _IL
                                    # Agent step-streaming passes a plain string. Push it live AND — for a node-agent
                                    # run — PERSIST it to the relay as its own chat message (like a DM), so leaving
                                    # mid-run and returning shows the play-by-play instead of a vanished log. (The
                                    # `logs` health report streams here too but delivers its own board, so skip it.)
                                    if isinstance(job, str):
                                        if _cmd == "node" and not _was_deleted(_conv):
                                            _pdb = SessionLocal()
                                            try:
                                                # Only persist to an EXISTING conversation — a progress line must never
                                                # resurrect a deleted/never-made chat (that's the final-result's job).
                                                if _pdb.query(_Conv).filter(_Conv.id == _conv).first():
                                                    _pu = _pdb.query(User).filter(User.id == _uid).first()
                                                    await chat_history.append(_pdb, _pu, _conv, "assistant", job)
                                            except Exception as _e:
                                                logger.warning(f"[node] webui step persist failed: {_e}")
                                                _pdb.rollback()
                                            finally:
                                                _pdb.close()
                                        try:
                                            await manager.send_json(_uid, {"type": "response", "data": {"type": "text", "content": job}}, _conn, _conv)
                                        except Exception as _e:
                                            logger.warning(f"[node] webui step notify failed: {_e}")
                                        return
                                    # Live "working… step N/M" heartbeat (ephemeral — a single updating pill, never
                                    # persisted) so a slow multi-minute run never looks dead between model-load gaps.
                                    if isinstance(job, dict) and job.get("type") == "agent_progress":
                                        try:
                                            await manager.send_json(_uid, {"type": "agent_progress", "step": job.get("step"),
                                                                            "max": job.get("max"), "node": job.get("node")}, _conn, _conv)
                                        except Exception:
                                            pass
                                        return
                                    # The user DELETED this chat (its agent was cancelled, but a finish can race the
                                    # delete). Drop the result rather than RESURRECT the conversation they just removed.
                                    if _was_deleted(_conv):
                                        return
                                    # Background AGENT finished — persist its summary to the conversation and
                                    # push it (queued if the socket is gone), so a run the user walked away
                                    # from still lands here when it's done. Same delivery shape as a job.
                                    if isinstance(job, dict) and job.get("type") == "agent_result":
                                        _atext = (job.get("content") or "").strip() or "(the agent produced no summary)"
                                        _adb = SessionLocal()
                                        try:
                                            # The panel opens a FRESH conversation per run; a multi-minute agent
                                            # outlives it and the row can be gone by the time the result lands
                                            # (deleted/pruned) → a bare insert FK-violates and the result is LOST
                                            # ("never came back"). Resurrect the exact conversation id (safe: the
                                            # id sequence has moved past it, so no collision) so the message
                                            # persists AND the client's live push to _conv still matches.
                                            _ac = _adb.query(_Conv).filter(_Conv.id == _conv).first()
                                            if not _ac:
                                                _ac = _Conv(id=_conv, user_id=_uid, title="🤖 Agent run")
                                                _adb.add(_ac)
                                                _adb.flush()
                                            _ac.updated_at = datetime.utcnow()
                                            _adb.commit()
                                            # Persist the message to the RELAY (chat_store) — the source of truth the
                                            # client reads in relay-backed mode. A bare SQL `messages` insert shows
                                            # live over the socket then VANISHES on reload / chat-switch (the bug).
                                            _uu = _adb.query(User).filter(User.id == _uid).first()
                                            await chat_history.append(_adb, _uu, _conv, "assistant", _atext)
                                        except Exception as _e:
                                            logger.warning(f"[node] webui agent-result save failed: {_e}")
                                            _adb.rollback()
                                        finally:
                                            _adb.close()
                                        try:
                                            await manager.send_json(_uid, {"type": "response", "data": {"type": "text", "content": _atext}}, _conn, _conv)
                                            await manager.send_json(_uid, {"type": "stream_end"}, _conn)
                                        except Exception as _e:
                                            logger.warning(f"[node] webui agent-result send failed: {_e}")
                                        # A background agent finishes minutes later, and the user has usually navigated
                                        # away from the launch chat — so the result above is queued/invisible and they
                                        # have "no idea when it finished or failed". Broadcast a completion signal to ALL
                                        # the user's sockets (conn_id=None) so the client toasts it whatever they're viewing.
                                        try:
                                            # Base success on run_agent's EXACT end-of-run markers, not loose
                                            # substrings like "Error"/"Stopped" (a fine summary that says "no
                                            # errors" or "Stopped the service" would otherwise toast as failed).
                                            _ok = not any(_m in _atext for _m in
                                                          ("**⏹️ Stopped:**", "**⚠️ Stopped:**", "**⚠️ Error:**"))
                                            await manager.send_json(_uid, {"type": "agent_done", "ok": _ok, "conv": _conv})
                                        except Exception:
                                            pass
                                        return
                                    # Background agent handed back FILES (e.g. its /workspace backup): save each blob
                                    # to encrypted Blossom, append a download link, persist + push — the same store the
                                    # `type:files` command path uses, so it survives reload and shows in AI files.
                                    if isinstance(job, dict) and job.get("type") == "agent_files":
                                        _ftext = (job.get("content") or "").strip()
                                        _fdb = SessionLocal()
                                        # Workspace backups are TRANSIENT snapshots — give them a bounded TTL
                                        # (agent_artifact_ttl_days) so a run-every-time auto-archive can't fill
                                        # storage. The download link works until it expires + gets swept.
                                        try:
                                            from app.services import settings_store as _ss_ttl
                                            _art_ttl = int(_ss_ttl.get("agent_artifact_ttl_days", "14") or 14)
                                        except Exception:
                                            _art_ttl = 14
                                        try:
                                            _links = []
                                            for _bf in job.get("files", []):
                                                _bd = _bf.get("data")
                                                if not _bd:
                                                    continue
                                                _bn = _bf.get("filename", "workspace.tar.gz")
                                                _ext = (_bn.rsplit(".", 1)[-1] if "." in _bn else "bin")
                                                _rel = await _save_artifact_blossom(_uid, _conv, _bd, _ext, expires_days=_art_ttl)
                                                if _rel:
                                                    _links.append(f"[⬇️ {_bn}](/api/files/{_q(_uname, safe='')}/{_conv}/{_q(Path(_rel).name)})")
                                            if _links:
                                                _ftext = (_ftext + "\n\n" + "\n".join(_links)).strip()
                                            if _ftext:
                                                _fc = _fdb.query(_Conv).filter(_Conv.id == _conv).first()
                                                if _fc:   # don't resurrect a chat just for a backup; the result already did
                                                    _fu = _fdb.query(User).filter(User.id == _uid).first()
                                                    await chat_history.append(_fdb, _fu, _conv, "assistant", _ftext)
                                                    await manager.send_json(_uid, {"type": "response", "data": {"type": "text", "content": _ftext}}, _conn, _conv)
                                        except Exception as _e:
                                            logger.warning(f"[node] webui agent-files save failed: {_e}")
                                            _fdb.rollback()
                                        finally:
                                            _fdb.close()
                                        return
                                    _icon = {"done": "✅", "failed": "❌", "killed": "🛑"}.get(job.status, "ℹ️")
                                    _out = (job.output or "(no output)").strip()
                                    _text = f"{_icon} Job #{job.id} on `{job.node}` {job.status} (exit {job.exit_code})\n\n```\n{_tail(_out, _IL)}\n```"
                                    _db = SessionLocal()
                                    try:
                                        # Long output: save the full thing and link it (inline shows only the tail).
                                        if len(_out) > _IL:
                                            try:
                                                _ob = (job.output or "").encode("utf-8", "replace")
                                                if chat_store.enabled(_db):
                                                    _nu = _db.query(User).filter(User.id == _uid).first()
                                                    _rel = await artifact_store.save_bytes(_db, _nu, _conv, _ob, "txt")
                                                else:
                                                    _rel = StorageService(_db).save_file_bytes(
                                                        _uname, _conv, _ob, f"node-{job.node}-job{job.id}.txt")
                                                _saved = _rel.replace("\\", "/").split("/")[-1]
                                                _text += f"\n\n[⬇️ full output](/api/files/{_q(_uname, safe='')}/{_conv}/{_q(_saved)})"
                                            except Exception as _fe:
                                                logger.warning(f"[node] webui full-output save failed: {_fe}")
                                        # Resurrect the conversation if a long job outlived it (same reason as
                                        # the agent-result branch above) so the finished job's output is never lost.
                                        _c = _db.query(_Conv).filter(_Conv.id == _conv).first()
                                        if not _c:
                                            _c = _Conv(id=_conv, user_id=_uid, title="🛰️ Node job")
                                            _db.add(_c)
                                            _db.flush()
                                        _c.updated_at = datetime.utcnow()
                                        _db.commit()
                                        # Message → RELAY (chat_store), not the SQL messages table, so it survives
                                        # a reload/chat-switch in relay-backed mode (matches the agent-result branch).
                                        _uu2 = _db.query(User).filter(User.id == _uid).first()
                                        await chat_history.append(_db, _uu2, _conv, "assistant", _text)
                                    except Exception as _e:
                                        logger.warning(f"[node] webui notify save failed: {_e}")
                                        _db.rollback()
                                    finally:
                                        _db.close()
                                    try:
                                        await manager.send_json(_uid, {"type": "response", "data": {"type": "text", "content": _text}}, _conn, _conv)
                                        await manager.send_json(_uid, {"type": "stream_end"}, _conn)
                                    except Exception as _e:
                                        logger.warning(f"[node] webui notify send failed: {_e}")
                                # Tag the closure with its launch conversation so deleting that chat can
                                # find + cancel a still-running background agent (system.cancel_agent_for_conv).
                                node_notify.conv_id = conversation_id

                            # Keep the socket alive across a long render. Effects/video run in a worker
                            # thread (asyncio.to_thread), so the event loop is FREE here — but with no
                            # traffic flowing, nginx/Cloudflare close the WS on their idle read timeout
                            # (~60s/~100s). That loses the live result frame and forces the slow recovery
                            # path — exactly why FAST image effects work but SLOW video effects "never
                            # update until refresh". A ping every 20s keeps the connection active; the
                            # client ignores {type:ping} (and treats any frame as "still alive").
                            async def _keepalive():
                                try:
                                    while True:
                                        await asyncio.sleep(20)
                                        await websocket.send_json({"type": "ping"})
                                except Exception:
                                    pass
                            _ka = asyncio.create_task(_keepalive())
                            _mark_busy(conversation_id)
                            try:
                                result = await command_service.execute_command(
                                    command, arg, last_prompt,
                                    stop_check=should_stop_command,
                                    attachments=mail_attachments or media_attachments,
                                    node_notify=node_notify,
                                )
                            finally:
                                _ka.cancel()
                                _clear_busy(conversation_id)

                            # Check if stopped during execution
                            if manager.should_stop(user.id, conn_id, conversation_id):
                                logger.debug("Command stopped during execution")
                                manager.set_stop(user.id, False, conversation_id)
                                continue

                            logger.debug(f"Command result type: {result.get('type')}")
                        except Exception as cmd_err:
                            logger.error(f"Command execution failed: {type(cmd_err).__name__}: {cmd_err}", exc_info=True)
                            # Rollback any uncommitted transaction to prevent session corruption
                            try:
                                db.rollback()
                            except Exception:
                                pass
                            result = {"type": "text", "content": f"Error: {cmd_err}"}

                        # compress/convert produce binary files — save them to storage
                        # and replace the raw bytes with markdown download links so the
                        # result is JSON-serializable for the websocket.
                        # Normalize + persist the command output via the SHARED helper (also used by the
                        # HTTP /chat/send fallback so the two can't drift). It rewrites a 'files' result to
                        # inline-markdown text for the live push, saves a generated image, and appends the
                        # flashcards [[FC]] marker — returning what to persist and what to send live.
                        _save_content, generated_image_path, result = await normalize_command_result(
                            db, user, conversation_id, result, storage_service)

                        # Save assistant response with image path.
                        # A long command (e.g. flashcards: fetch + LLM building 20+ cards) can idle the
                        # DB transaction past Postgres' idle_in_transaction_session_timeout (60s), so the
                        # connection is dead by the time we INSERT here ("server closed the connection
                        # unexpectedly") and the reply persists NOWHERE — gone on reload. Retry once: the
                        # rollback discards the dead connection, and pool_pre_ping hands the retry a fresh
                        # one. (This is also what mirrors the message to the relay, the sole read store.)
                        assistant_msg = None
                        for _attempt in (1,):
                            try:
                                assistant_msg = await chat_history.append(
                                    db, user, conversation_id, "assistant", _save_content,
                                    image_path=generated_image_path)
                                break
                            except Exception as save_err:
                                logger.error(f"Failed to save assistant message (attempt {_attempt}): {save_err}")
                                assistant_msg = None
                                try:
                                    db.rollback()   # discards a dead connection → retry gets a fresh one
                                except Exception:
                                    pass

                        # Send response (with conn_id to ensure it goes to correct chat, queue if stale)
                        # Log image generation responses for debugging
                        if result.get("type") == "generated_image":
                            image_len = len(result.get("image", "")) if result.get("image") else 0
                            logger.info(f"[WEBSOCKET] Sending generated_image response: image_length={image_len}, has_prompt={bool(result.get('prompt'))}")
                        
                        # DIAGNOSTIC (temporary): tells us, per command result, whether the live push will be
                        # DELIVERED or QUEUED — i.e. is the socket still in the registry, and is conn_id still
                        # the current one for this conversation (a mismatch = it went stale during the render).
                        _cur = manager.connection_ids.get((user.id, conversation_id))
                        logger.info("[CHAT-DELIVER] conv=%s type=%s conn=%s current=%s ws_alive=%s → %s",
                                    conversation_id, result.get("type"), conn_id, _cur,
                                    conn_id in manager.active_connections,
                                    "DELIVER" if (_cur == conn_id and conn_id in manager.active_connections) else "QUEUE")
                        await manager.send_json(user.id, {
                            "type": "response",
                            "data": result,
                            # This reply was written to the DB above (when assistant_msg committed), so a
                            # web client that reloads the conversation on reconnect already has it —
                            # mark it so a queued REPLAY is skipped instead of double-rendered.
                            "persisted": assistant_msg is not None,
                        }, conn_id, conversation_id)
                        # Signal end of response so TUI stops waiting
                        await manager.send_json(user.id, {"type": "stream_end"}, conn_id)
                    else:
                        # Check if intent detection is enabled
                        intent_enabled = settings_store.get("intent_detection_enabled")
                        intent_enabled = (intent_enabled if intent_enabled is not None else "true").lower() == "true"

                        if intent_enabled:
                            # Try AI-powered intent detection first
                            # Build context from file content/OCR for intent analysis
                            intent_context = ""
                            if file_content:
                                intent_context = file_content
                            elif image_data:
                                # Extract OCR for intent detection
                                ocr_for_intent = extract_image_text(image_data)
                                if ocr_for_intent:
                                    intent_context = ocr_for_intent

                            try:
                                intent_result = await intent_service.detect_intent(content, intent_context)
                                if intent_result and intent_result.get("action") != "none":
                                    logger.info(f"Intent detected: {intent_result['action']} (confidence: {intent_result.get('confidence', 0):.2f})")

                                    # Execute the detected intent
                                    action_result = await intent_service.execute_intent(intent_result)

                                    if action_result:
                                        # Check if stopped during execution
                                        if manager.should_stop(user.id, conn_id, conversation_id):
                                            logger.debug("Intent action stopped during execution")
                                            manager.set_stop(user.id, False, conversation_id)
                                            continue

                                        # Save assistant response
                                        await chat_history.append(db, user, conversation_id, "assistant",
                                                                  action_result.get("content", ""))

                                        # Send response
                                        await manager.send_json(user.id, {
                                            "type": "response",
                                            "data": action_result
                                        }, conn_id, conversation_id)
                                        # Signal end of response so TUI stops waiting
                                        await manager.send_json(user.id, {"type": "stream_end"}, conn_id)
                                        continue  # Skip regular chat since action was taken
                            except Exception as intent_err:
                                logger.debug(f"Intent detection skipped: {intent_err}")
                                # Fall through to regular chat on any error

                        # Regular chat - stream response
                        # Wrap in try-finally to ensure stream_end is always sent
                        try:
                            # Build message history (exclude the just-added user message)
                            # Replace date placeholder in system prompt
                            system_prompt = chat_service.system_prompt.replace(
                                "{{CURRENT_DATE}}", datetime.utcnow().strftime("%Y-%m-%d")
                            )

                            messages = [
                                {"role": "system", "content": system_prompt}
                            ]
                            # Get last 19 messages (excluding the one we just added)
                            # Sort by ID to ensure correct order (timestamps can be identical)
                            # Conversation memory: the ENCRYPTED relay transcript (no plaintext rows).
                            # _prior was loaded before this turn was appended, so it is already
                            # "everything except the in-flight message" — mirroring the old [-21:-1].
                            sorted_messages = list(_prior)
                            # Filter to ensure alternating roles (user/assistant/user/assistant)
                            # Truncate history messages to prevent context bloat from URL content
                            HISTORY_CHAR_LIMIT = 500
                            last_role = "system"

                            # Bare URL check: if the message is just a URL, treat it as
                            # a standalone summarization request — no history, no contamination.
                            _content_stripped = content.strip() if isinstance(content, str) else ""
                            _is_bare_url = (
                                isinstance(content, str) and
                                _content_stripped.startswith(("http://", "https://")) and
                                " " not in _content_stripped
                            )

                            # Context-sensitivity heuristic: short messages that don't
                            # reference prior conversation (via pronouns/follow-up words) are
                            # treated as standalone questions. Sending history for these causes
                            # the model to pattern-match on the previous topic (e.g. answering
                            # "2+2=" with "To convert 2+2 to days..."). Skip history in that case.
                            # Also skip history for bare URLs — they are independent summarization
                            # requests and history causes the model to produce garbage output.
                            _CONTEXT_REFS = {
                                "it", "that", "this", "they", "them", "which", "those", "these",
                                "previous", "last", "above", "said", "also", "more", "again",
                                "continue", "same", "instead", "another", "shorter", "longer",
                                "different", "decimal", "further", "else", "other",
                                # Personal/memory references. "What is my name?" is 17 chars and
                                # contains no word above, so it was treated as a standalone question
                                # and answered with no history at all — the user asking the single
                                # most obvious memory question and being told nothing.
                                "i", "me", "my", "mine", "we", "us", "our", "you", "your",
                                "remember", "forget", "earlier", "before", "told", "name",
                            }
                            # Split on WORD characters, not whitespace: `.split()` left punctuation
                            # attached, so "Where am I from again?" tokenised to "again?" and missed
                            # "again" entirely. Context words sit at the end of a question far more
                            # often than not, so this silently disabled history for exactly the
                            # follow-ups that need it most.
                            _current_words = (set(re.findall(r"[a-z0-9']+", content.lower()))
                                              if isinstance(content, str) else set())
                            _is_context_dependent = (
                                not _is_bare_url and
                                (bool(_current_words & _CONTEXT_REFS) or
                                len(content.strip()) > 60)
                            )

                            if _is_context_dependent:
                                for msg in sorted_messages[-20:]:
                                    _role = (msg.get("role") or "")
                                    # Skip if this role is same as last (prevents "Conversation roles must alternate" error)
                                    if not _role or _role == last_role:
                                        continue
                                    _c = msg.get("content") or ""
                                    messages.append({"role": _role, "content": _c[:HISTORY_CHAR_LIMIT]})
                                    last_role = _role

                            # Detect and fetch URLs in user message AND system prompt (with timeout to avoid hanging)
                            url_context = ""
                            urls = SearchService.extract_urls(content)
                            # Also extract URLs from system prompt (e.g., $xrp command URLs)
                            system_urls = SearchService.extract_urls(system_prompt)
                            for url in system_urls:
                                if url not in urls:
                                    urls.append(url)
                            # Deduplicate: www.example.com and example.com are the same article.
                            if urls:
                                import re as _re
                                def _url_key(u: str) -> str:
                                    return _re.sub(r'^https?://(www\.)?', '', u.lower().rstrip('/'))
                                _seen_keys: set = set()
                                _deduped: list = []
                                for u in urls:
                                    k = _url_key(u)
                                    if k not in _seen_keys:
                                        _seen_keys.add(k)
                                        _deduped.append(u)
                                if len(_deduped) < len(urls):
                                    logger.info(f"Deduplicated URLs {urls} -> {_deduped}")
                                urls = _deduped
                            if urls:
                                logger.info(f"Detected URLs in message: {urls}")
                                try:
                                    # Add 15 second timeout for URL fetching to avoid long delays
                                    fetched = await asyncio.wait_for(
                                        search_service.fetch_urls(urls, max_urls=3),
                                        timeout=15
                                    )
                                    for result in fetched:
                                        if result.get("content") and not result.get("error"):
                                            logger.info(f"Fetched {len(result['content'])} chars from {result['url']}")
                                            url_context += f"\n\n---\nContent from {result['url']}:\nTitle: {result['title']}\n\n{result['content']}\n---"
                                        elif result.get("error"):
                                            logger.warning(f"Failed to fetch {result['url']}: {result['error']}")
                                            url_context += f"\n\n[Failed to fetch {result['url']}: {result['error']}]"
                                except asyncio.TimeoutError:
                                    logger.warning(f"URL fetching timed out after 15s for URLs: {urls}")
                                    url_context = "\n\n[Note: Could not fetch URL content due to timeout]"
                            
                            # Get ACTUAL context size for intelligent truncation
                            actual_ctx = 4096
                            try:
                                from app.services.inference_factory import get_inference_service
                                service = get_inference_service(db)
                                actual_ctx = getattr(service, 'num_ctx', 4096)
                            except Exception:
                                pass
                            
                            # Reserve ~2500 tokens for system/history/user/response, use rest for URL content
                            max_url_chars = max(500, int(actual_ctx * 4) - 10000)
                            if len(url_context) > max_url_chars:
                                logger.info(f"Truncating URL context from {len(url_context):,} to {max_url_chars:,} chars")
                                url_context = url_context[:max_url_chars] + "\n\n[URL content truncated to fit context window]"

                            # Add current message with file/image content if provided
                            if image_data:
                                # Use OCR to extract text from image
                                ocr_text = extract_image_text(image_data)
                                if ocr_text:
                                    user_request = content if content else "Please provide a detailed, objective summary and analysis of this document."
                                    messages.append({
                                        "role": "user",
                                        "content": f"""The user uploaded an image containing the following text (extracted via OCR):

---BEGIN EXTRACTED TEXT---
{ocr_text}
---END EXTRACTED TEXT---

User's request: {user_request}

Please analyze the above text objectively and thoroughly. Provide a comprehensive summary covering the main points, key details, and any important information found in the document."""
                                    })
                                else:
                                    messages.append({
                                        "role": "user",
                                        "content": f"{content or 'The user uploaded an image.'} [Note: An image was uploaded but no text could be extracted from it. Please ask the user to describe what they see.]"
                                    })
                            elif file_content:
                                # Get ACTUAL context size (may be reduced from configured value due to memory)
                                context_size = 4096  # Safe default
                                try:
                                    from app.services.inference_factory import get_inference_service
                                    service = get_inference_service(db)
                                    context_size = getattr(service, 'num_ctx', 4096)
                                except Exception as e:
                                    logger.debug(f"Could not get service context size: {e}")
                                
                                # Use ~50% of context tokens as chars for file content
                                # (accounting for system/history/response overhead)
                                max_file_chars = int(context_size * 2.0)
                                
                                if len(file_content) > max_file_chars:
                                    logger.info(f"Truncating file content from {len(file_content):,} to {max_file_chars:,} chars (actual context: {context_size})")
                                    file_content = file_content[:max_file_chars] + "\n\n[File content truncated - document is too large for context window]"
                                
                                # If user message is just "summarize" or similar, make the instruction explicit
                                user_message_lower = (content or "").lower().strip()
                                summarize_keywords = ["summarize", "summarise", "summary", "summarie"]
                                is_summarize_request = any(keyword in user_message_lower for keyword in summarize_keywords) and len(user_message_lower.split()) <= 3
                                
                                file_msg = ""
                                if is_summarize_request or not content or len(content.strip()) < 5:
                                    file_msg = f"The user uploaded a file and asked you to summarize it. Please provide a comprehensive summary of the following file content:\n\n```\n{file_content}\n```\n\nProvide a detailed summary covering the main points, key information, and important details from the document."
                                else:
                                    file_msg = f"Here is a file the user uploaded:\n\n```\n{file_content}\n```\n\nUser's message: {content}"
                                
                                # If last_role is user, merge with last message instead of creating duplicate
                                if last_role == "user":
                                    messages[-1]["content"] += f"\n\n{file_msg}"
                                else:
                                    messages.append({"role": "user", "content": file_msg})
                            elif url_context:
                                logger.info(f"Adding {len(url_context)} chars of URL context to message")
                                if _is_bare_url:
                                    # Bare URL: instruction goes AFTER content; explicit anti-loop stop.
                                    user_msg_text = url_context + "\n\nWrite a single concise paragraph summarizing the above. Output ONLY the summary paragraph, then STOP."
                                else:
                                    user_msg_text = f"{content}\n\n[The following web content was fetched from URLs mentioned in the user's message:]{url_context}"
                                # If last_role is user, merge with last message instead of creating duplicate
                                if last_role == "user":
                                    messages[-1]["content"] += f"\n\n{user_msg_text}"
                                else:
                                    messages.append({
                                        "role": "user",
                                        "content": user_msg_text
                                    })
                            else:
                                # If last_role is user, merge with last message instead of creating duplicate
                                if last_role == "user":
                                    messages[-1]["content"] += f"\n\n{content}"
                                else:
                                    messages.append({"role": "user", "content": content})

                            # FINAL VALIDATION: Ensure messages alternate properly before sending to LLM
                            validated_messages = [messages[0]]  # Keep system message
                            for msg in messages[1:]:
                                if msg['role'] != validated_messages[-1]['role']:
                                    validated_messages.append(msg)
                                else:
                                    # Merge with previous message instead of skipping
                                    validated_messages[-1]['content'] += f"\n\n{msg['content']}"
                            messages = validated_messages
                            logger.info(f"Final message sequence: {[m['role'] for m in messages]}")

                            # Stream response with thinking tag filtering
                            # Import from central location for consistency
                            from app.services.text_utils import find_thinking_open
                            BUFFER_MARGIN = 20  # Enough for longest closing tag

                            full_response = ""
                            buffer = ""
                            in_thinking = False
                            current_close_tag = None  # Track which closing tag we're looking for

                            async for chunk in chat_service.chat_stream(messages):
                                # Check if user requested stop OR switched to another chat
                                if manager.should_stop(user.id, conn_id, conversation_id):
                                    break
                                full_response += chunk
                                buffer += chunk
                                logger.info(f"[STREAM] Chunk received, len={len(chunk)}, buffer_len={len(buffer)}")

                                # Filter out thinking content in real-time
                                while True:
                                    if not in_thinking:
                                        # Look for start of any thinking tag
                                        think_start, tag_pair = find_thinking_open(buffer)
                                        if think_start == -1:
                                            # No thinking tag, send buffered content (keep margin in case tag is split)
                                            # For very short responses, send immediately to avoid empty bubbles
                                            if len(buffer) > BUFFER_MARGIN:
                                                to_send = buffer[:-BUFFER_MARGIN]
                                                buffer = buffer[-BUFFER_MARGIN:]
                                                if to_send:
                                                    logger.info(f"[STREAM] Sending chunk, len={len(to_send)}")
                                                    await manager.send_json(user.id, {
                                                        "type": "stream",
                                                        "data": {"content": to_send}
                                                    }, conn_id)
                                            # If buffer is small but we have content, send it immediately
                                            elif len(buffer) > 0:
                                                to_send = buffer
                                                buffer = ""
                                                logger.info(f"[STREAM] Sending small chunk immediately, len={len(to_send)}")
                                                await manager.send_json(user.id, {
                                                    "type": "stream",
                                                    "data": {"content": to_send}
                                                }, conn_id)
                                            break
                                        else:
                                            # Found opening tag, send content before it and enter thinking mode
                                            if think_start > 0:
                                                await manager.send_json(user.id, {
                                                    "type": "stream",
                                                    "data": {"content": buffer[:think_start]}
                                                }, conn_id)
                                            open_tag, close_tag = tag_pair
                                            buffer = buffer[think_start + len(open_tag):]
                                            current_close_tag = close_tag
                                            in_thinking = True
                                    else:
                                        # In thinking mode, look for matching end tag
                                        think_end = buffer.lower().find(current_close_tag)
                                        if think_end == -1:
                                            # Still in thinking, discard buffered thinking content but keep margin
                                            if len(buffer) > BUFFER_MARGIN:
                                                buffer = buffer[-BUFFER_MARGIN:]
                                            break
                                        else:
                                            # Found closing tag, exit thinking mode
                                            buffer = buffer[think_end + len(current_close_tag):]
                                            current_close_tag = None
                                            in_thinking = False

                            # Send any remaining buffered content
                            if buffer and not in_thinking:
                                logger.info(f"[STREAM] Sending final buffer, len={len(buffer)}")
                                await manager.send_json(user.id, {
                                    "type": "stream",
                                    "data": {"content": buffer}
                                }, conn_id)

                            # Ensure we always send stream_end, even if there was an error or stop request
                            logger.info(f"[STREAM] Complete, total_len={len(full_response)}, stopped={manager.should_stop(user.id, conn_id, conversation_id)}")
                            
                            # Always send stream_end, even if full_response is empty
                            await manager.send_json(user.id, {"type": "stream_end"}, conn_id)

                            # Save assistant response
                            if full_response:
                                clean_response = chat_service.strip_thinking_tags(full_response)

                                # Save assistant response
                                await chat_history.append(db, user, conversation_id, "assistant", clean_response)

                        except Exception as stream_err:
                            logger.error(f"Error during streaming: {stream_err}", exc_info=True)
                            # Try to send error message to client
                            try:
                                await manager.send_json(user.id, {
                                    "type": "stream",
                                    "data": {"content": f"\n\n[Error: {str(stream_err)}]"}
                                }, conn_id)
                            except Exception:
                                pass
                        finally:
                            # Always send stream_end to prevent UI hanging
                            await manager.send_json(user.id, {"type": "stream_end"}, conn_id)

        except WebSocketDisconnect:
            if user:
                manager.disconnect(user.id, conversation_id, conn_id, websocket)
    finally:
        if db:
            # The WS holds this session for the whole connection; a long idle stretch (e.g. a background
            # agent the user is waiting on) lets Postgres close it, and then db.close() → rollback raises
            # OperationalError, spamming a scary traceback on every such disconnect. Swallow it.
            try:
                db.close()
            except Exception:
                pass
