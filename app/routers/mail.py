"""
Mail Router - API endpoints for email functionality.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, FileResponse
from sqlalchemy.orm import Session

from pathlib import Path
from urllib.parse import unquote

from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services.mail_service import (get_attachment, get_user_mail_accounts, sanitize_filename,
                                       send_email, reply_to_message, forward_message,
                                       archive_message, delete_message, move_message,
                                       list_special_folders as _list_special_folders)
from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mail", tags=["mail"])


from pydantic import BaseModel


class MailAiReq(BaseModel):
    mode: str                    # 'summarize' | 'reply'
    text: str                    # the email, as plain text (subject/from/date headers + body)
    instruction: str | None = None   # reply mode: how the user wants it answered
    myName: str | None = None    # the user's own name (the To header's display name), for the sign-off


@router.post("/ai")
async def mail_ai(req: MailAiReq, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """The ✨ AI menu on an open email: summarize it, or draft a reply to it.

    THE MODEL ONLY EVER PRODUCES TEXT THE USER THEN REVIEWS. A summary is displayed; a reply draft
    lands in the composer with the Send button untouched — nothing here sends mail, files bills, or
    acts on the message. (The third menu entry, Add to Budget, is /api/budget/scan — the same
    pipeline as "Add Bill with AI", fed the email as text.)

    Thin on purpose, like budget_scan: the model plumbing is CommandService's chat_service, which
    already knows about the LB, the proxy and per-user access."""
    text = (req.text or "").strip()[:16000]
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    mode = (req.mode or "").strip().lower()
    if mode not in ("summarize", "reply"):
        raise HTTPException(status_code=400, detail="mode must be summarize or reply")
    from app.services.command_service import CommandService
    cs = CommandService(db, user=current_user)
    # Task temperature, not chat temperature. At the default 0.7 the same instruction drafted a
    # different reply every run ("looking better 2nd and 4th time"); at 0.2 two runs agree in
    # substance and differ only in phrasing. This is a drafting tool, not a muse.
    try:
        cs.chat_service.temperature = 0.2
    except Exception:
        pass
    if mode == "summarize":
        msgs = [
            {"role": "system", "content": (
                "Summarize this email in a few short plain-text bullet points. Lead with what it "
                "is and what, if anything, the reader must DO — name any amounts, dates and "
                "deadlines exactly as written. No preamble, no markdown headings.")},
            {"role": "user", "content": text},
        ]
    else:
        instr = (req.instruction or "").strip()[:1000]
        if not instr:
            raise HTTPException(status_code=400, detail="say how to reply")
        myname = (req.myName or "").strip().strip('"<>').strip()[:60]
        # EMAIL FIRST, INSTRUCTION LAST, and an explicit cue to begin. The first shape put the
        # instruction at the top and the email underneath — and the local model, recency-biased,
        # CONTINUED the email instead of replying to it: a payroll summary came back verbatim as
        # "the reply", subject line and all. Reproduced against the live model, then reworked and
        # re-proven on three instruction shapes (short thanks / polite decline / yes-plus-question)
        # before this landed. The fence marks the email as quoted material, not text to extend.
        msgs = [
            # …and the instruction is what the reply should CONVEY, never text to paste: the
            # first fix stopped the model echoing the EMAIL, and then "Thanks!" as an instruction
            # came back as literally "Thanks!" — the parrot one level up. Demanding a natural
            # response that refers to what the sender wrote is what turned it into "Thanks! That's
            # very helpful. Have a great weekend!" (probed live on content-shaped, directive-shaped
            # and nonsense instructions before landing).
            # TWO RULES BORN OF REAL DRAFTS. "I've called the front desk and scheduled the
            # Mid-Year Review for next Tuesday at 10 AM" — a fabricated past-tense action over a
            # vague "will do"; and "Best, Jordan" — a name that belongs to nobody, invented when
            # the To line had no display name. Commitments may only come from the instruction, and
            # the sign-off is GROUNDED: the client sends the user's name when it knows it, and with
            # none the reply ends after the last content sentence (enforced again in code below).
            {"role": "system", "content": (
                "You write email replies. Rules: never repeat or continue the original email; "
                "never paste the instruction into the reply — it only says what the reply should "
                "CONVEY; the reply refers to what the sender actually wrote (at least one full "
                "sentence). NEVER INVENT COMMITMENTS: no dates, times, deadlines, meetings, or "
                "promised actions unless the instruction states them — when the instruction gives "
                "no specifics, stay unspecific. "
                + (("If a sign-off fits, sign exactly as: " + myname + ". Never any other name. ")
                   if myname else
                   "Do not add any sign-off, valediction or name at the end — stop after the "
                   "last content sentence. ")
                + "Output only the body of the NEW reply, plain text: no subject line, no "
                "quoting, no commentary. Match the sender's language unless the instruction says "
                "otherwise.")},
            {"role": "user", "content": "An email I received:\n<<<EMAIL\n" + text
                + "\nEMAIL\n\nWrite my reply. What it should convey: " + instr
                + "\n\nMy reply (a natural response to what the sender wrote):"},
        ]
    try:
        out = await cs.chat_service.chat(msgs) or ""
    except Exception as e:
        logger.warning("[MAIL] ai %s failed: %s", mode, e)
        raise HTTPException(status_code=502, detail="the model did not answer")
    out = out.strip()
    # Belt over the prompt's braces: a placeholder signature still slips out sometimes, and a rule
    # a model follows most of the time is a rule — a line of code is a guarantee. Trailing
    # `[Anything]` lines are stripped, plus a valediction left orphaned directly above one.
    import re as _re
    cleaned = _re.sub(r"(\n\s*(?:best|regards|thanks|sincerely|cheers)[,!.]?\s*)?\n\s*\[[^\]\n]{1,40}\]\s*$",
                      "", out, flags=_re.I).rstrip()
    # With NO name known, any trailing valediction+name block is an invention ("Best,\nJordan" —
    # signed as somebody who does not exist). Strip it whole; with a name known, the model was told
    # exactly what to sign and a real signature is left alone.
    if mode == "reply" and not (req.myName or "").strip():
        cleaned = _re.sub(r"\n\s*(?:best|regards|thanks|thank you|sincerely|cheers|warmly)[,!.]?\s*\n\s*\S[^\n]{0,30}\s*$",
                          "", cleaned, flags=_re.I).rstrip()
    out = cleaned or out
    if not out:
        raise HTTPException(status_code=502, detail="the model did not answer")
    return {"ok": True, "content": out}


@router.get("/attachment/{account_hint}/{uid}/{index}")
async def download_attachment(
    account_hint: str,
    uid: str,
    index: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Download an email attachment."""
    accounts = get_user_mail_accounts(current_user.id, db)
    account_email = None
    for acc in accounts:
        if account_hint.lower() in acc.email.lower():
            account_email = acc.email
            break

    if not account_email:
        raise HTTPException(status_code=404, detail="Account not found")

    attachment = get_attachment(current_user.id, db, account_email, uid, index)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    content_type = attachment.content_type.lower() if attachment.content_type else ''
    viewable_types = ['application/pdf', 'image/', 'text/plain', 'text/html']
    disposition = 'inline' if any(t in content_type for t in viewable_types) else 'attachment'

    safe_filename = sanitize_filename(attachment.filename)

    return Response(
        content=attachment.data,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_filename}"'
        }
    )


@router.get("/attachment/{username}/{filename:path}")
async def serve_saved_attachment(
    username: str,
    filename: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Serve a saved mail attachment from temp directory (opens in browser)."""
    # Decode URL-encoded username (handles @ symbols, etc.)
    from urllib.parse import unquote
    try:
        decoded_username = unquote(username)
        logger.debug(f"Serving mail attachment: username={username} (decoded={decoded_username}), filename={filename}")
    except Exception as e:
        logger.warning(f"Error decoding username {username}: {e}")
        decoded_username = username
    
    # Verify user owns this file (username must match after decoding)
    if current_user.username != decoded_username:
        # Try URL-encoding the current username to see if it matches
        from urllib.parse import quote
        encoded_current = quote(current_user.username, safe='')
        if encoded_current != username and current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
        decoded_username = current_user.username
    
    # Decode URL-encoded filename
    try:
        decoded_filename = unquote(filename)
    except Exception:
        decoded_filename = filename
    
    # Sanitize filename
    try:
        safe_filename = _sanitize_path_component(decoded_filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid filename: {str(e)}")
    
    # Get storage service and construct path using decoded username
    storage = StorageService(db)
    file_path = Path(storage.upload_path) / decoded_username / "temp" / "mail_attachments" / safe_filename
    
    # Validate path is within expected directory
    base_path = Path(storage.upload_path) / decoded_username / "temp" / "mail_attachments"
    logger.debug(f"Looking for mail attachment: file_path={file_path}, base_path={base_path}, base_exists={base_path.exists()}")
    
    if not _validate_path_within_base(file_path, base_path):
        logger.error(f"Invalid file path (path traversal attempt?): {file_path} not within {base_path}")
        raise HTTPException(status_code=403, detail="Invalid file path")
    
    if not file_path.exists():
        # Try to find the file with case-insensitive or partial matching
        if base_path.exists():
            logger.warning(f"Mail attachment not found: {safe_filename}, checking directory: {base_path}")
            
            try:
                files_in_dir = [f.name for f in base_path.iterdir() if f.is_file()]
                logger.warning(f"Files in mail_attachments directory: {files_in_dir[:10]}")
                
                # Try case-insensitive match
                for f in files_in_dir:
                    if f.lower() == safe_filename.lower():
                        logger.warning(f"Found case-insensitive match: {f} vs {safe_filename}")
                        file_path = base_path / f
                        break
                
                # If still not found, try partial match (filename contains the requested name)
                if not file_path.exists():
                    safe_base = Path(safe_filename).stem.lower()
                    safe_ext = Path(safe_filename).suffix.lower()
                    
                    for f in files_in_dir:
                        f_base = Path(f).stem.lower()
                        f_ext = Path(f).suffix.lower()
                        
                        # Match if extension matches and base name is contained
                        if f_ext == safe_ext and (safe_base in f_base or f_base in safe_base):
                            logger.warning(f"Found partial match: {f} vs {safe_filename}")
                            file_path = base_path / f
                            break
            except Exception as e:
                logger.error(f"Error listing mail_attachments directory: {e}", exc_info=True)
        
        # If still not found after matching attempts, raise 404
        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"Attachment not found: {safe_filename}")
    
    # Determine content type
    suffix = file_path.suffix.lower()
    content_type_map = {
        '.pdf': 'application/pdf',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.txt': 'text/plain',
        '.html': 'text/html',
        '.htm': 'text/html',
    }
    content_type = content_type_map.get(suffix, 'application/octet-stream')
    
    # Use inline disposition for viewable types (images, PDFs, text)
    viewable_types = ['application/pdf', 'image/', 'text/']
    disposition = 'inline' if any(t in content_type for t in viewable_types) else 'attachment'
    
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        filename=safe_filename,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_filename}"'
        }
    )


# ============================================================================
# Nostr-mailbox GUI API (Discover → Email). The mailbox lives as encrypted
# kind-30078 events (mail_store); IMAP/SMTP run server-side (mail_service);
# attachments are AES-GCM-encrypted in Blossom (mail_sync). All endpoints are
# per-user (JWT from the Nostr login). See docs in mail_store/mail_sync.
# ============================================================================
import base64
import asyncio as _asyncio
from fastapi import Request
from fastapi.responses import JSONResponse
from app.services import mail_store, mail_sync, nostr_store, mail_service
from app.services.nostr import nostr_service as _ns


def _seckey(db, user):
    return nostr_store.user_storage_seckey(db, user)


def _resolve_account(db, user, hint: str):
    """Match an account: EXACT email first (so 'a@x' can't resolve to 'aa@x'), then substring, then
    the first account when hint is blank."""
    accts = get_user_mail_accounts(user.id, db)
    if not hint:
        return accts[0] if accts else None
    hl = hint.lower()
    for a in accts:
        if a.email.lower() == hl:
            return a
    for a in accts:
        if hl in a.email.lower():
            return a
    return None


def _summary(m: dict) -> dict:
    """List-view projection — no bodies (those load on open), with an unread + attachment flag."""
    return {
        "uid": m.get("uid"), "account": m.get("account"), "folder": m.get("folder"),
        "from": m.get("from"), "from_email": m.get("from_email"), "to": m.get("to"),
        "subject": m.get("subject"), "date": m.get("date"), "ts": m.get("ts", 0),
        "read": bool((m.get("flags") or {}).get("read")),
        "attachments": len(m.get("attachments") or []),
        "preview": (m.get("preview") or m.get("body_text") or "")[:140],
    }


@router.get("/accounts")
async def mail_accounts(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return {"accounts": [{"email": a.email} for a in get_user_mail_accounts(current_user.id, db)]}


@router.post("/sync")
async def mail_do_sync(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Fast new-mail refresh: mirror INBOX for every account.

    Other folders refresh when opened. Walking every Archive/Trash/custom folder here made the
    foreground refresh wait behind unrelated mailboxes and counted newly mirrored Sent mail as a
    new incoming-message notification.
    """
    try:
        res = await mail_sync.sync_all(db, current_user, folders=["INBOX"])
        return {"ok": True, "new": res}
    except Exception as e:
        logger.warning("[mail] sync failed for %s: %s", current_user.id, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/messages")
async def mail_messages(account: str = "", folder: str = "INBOX", until: int = 0,
                        db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """One page of a folder, newest first. `until` is the cursor from the previous page."""
    sk = _seckey(db, current_user)
    if account == "__all":
        # Unified view: this logical folder across EVERY account. Ask each account for the folder(s)
        # that play the role rather than pulling the whole mailbox and filtering in Python — the
        # relay clamps any limit to 5000, so "read everything and filter" silently returned the
        # newest 5000 documents ACROSS ALL FOLDERS and showed whatever survived.
        msgs, nexts = [], []
        for acc in (get_user_mail_accounts(current_user.id, db) or []):
            names = [folder]
            if folder != "INBOX":
                try:
                    meta = await _asyncio.to_thread(_list_special_folders, current_user.id, db, acc.email)
                    key = {"Sent": "sent", "Drafts": "drafts", "Trash": "trash",
                           "Spam": "junk", "Archive": "archive"}.get(folder)
                    real = (meta.get(key) if key else None)
                    # BOTH names, not one. A compose draft is saved locally under the literal
                    # "Drafts" while the server's own drafts mailbox may be "[Gmail]/Drafts" or
                    # "INBOX.Drafts" — resolving to the real name alone made every draft written in
                    # this app disappear from All inboxes → Drafts.
                    if real and real != folder:
                        names.append(real)
                except Exception:
                    pass
            for name in names:
                try:
                    page, nxt = await mail_store.list_page(sk, acc.email, name, until=until or None)
                    msgs += page
                    if nxt:
                        nexts.append(nxt)
                except Exception as e:
                    logger.warning("[mail] unified view: %s/%s unreadable: %s", acc.email, name, e)
        seen, uniq = set(), []
        for m in sorted(msgs, key=lambda m: m.get("ts", 0), reverse=True):
            k = (m.get("account"), m.get("folder"), m.get("uid"))
            if k in seen:
                continue
            seen.add(k)
            uniq.append(m)
        # The OLDEST cursor across the accounts, so "Load older" keeps every account advancing
        # rather than stopping at whichever ran out first.
        return {"messages": [_summary(m) for m in uniq], "account": "__all",
                "next_until": (min(nexts) if nexts else None)}
    acc = _resolve_account(db, current_user, account)
    if not acc:
        return {"messages": [], "account": None}
    msgs, nxt = await mail_store.list_page(sk, acc.email, folder, until=until or None)
    return {"messages": [_summary(m) for m in msgs], "account": acc.email, "next_until": nxt}


@router.get("/message")
async def mail_message(account: str, uid: str, folder: str = "INBOX",
                       db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    acc = _resolve_account(db, current_user, account)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    sk = _seckey(db, current_user)
    msg = await mail_store.get_message(sk, acc.email, folder, uid)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.get("body_ref"):     # large body was offloaded to an encrypted Blossom blob — rehydrate it
        body = await mail_sync.load_body(db, msg["body_ref"])
        if body:
            msg["body_text"] = body.get("body_text", "")
            msg["body_html"] = body.get("body_html", "")
    if msg.get("is_draft"):     # draft attachments offloaded to Blossom → rehydrate to b64 for the composer
        for a in (msg.get("attachments") or []):
            if a.get("sha256") and not a.get("b64"):
                data, _ = await mail_sync.load_attachment(db, a)
                if data is not None:
                    a["b64"] = base64.b64encode(data).decode()
    if not (msg.get("flags") or {}).get("read"):     # opening marks it read
        await mail_store.set_flags(sk, acc.email, folder, uid, read=True)
        msg.setdefault("flags", {})["read"] = True
    return {"message": msg}


@router.get("/search")
async def mail_search(q: str, account: str = "", folder: str = "",
                      db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    sk = _seckey(db, current_user)
    # PAGED, for the reason /thread is. `list_messages(..., limit=0)` is ONE page and the relay
    # clamps it to 5000: measured on the reporting mailbox, unified search saw 5,000 of 17,921
    # documents — 3 of 2,717 archived messages and 1 of 807 in INBOX.Sent — and a single account
    # over the cap lost 449 of its 5,469. A search that only looks at the newest page is not a
    # search, and it fails SILENTLY: a truncated read is indistinguishable from "no matches".
    if account == "__all":
        msgs = await mail_store.list_all_messages(sk, None, None)
    else:
        acc = _resolve_account(db, current_user, account)
        msgs = await mail_store.list_all_messages(sk, acc.email if acc else None, folder or None)
    return {"messages": [_summary(m) for m in mail_store.search(msgs, q)]}


@router.post("/mark-read")
async def mail_mark_read(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    ok = await mail_store.set_flags(_seckey(db, current_user), acc.email, d.get("folder", "INBOX"),
                                    d.get("uid"), read=bool(d.get("read", True)))
    return {"ok": ok}


@router.post("/delete")
async def mail_delete(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    folder, uid = d.get("folder", "INBOX"), d.get("uid")
    if folder != "Drafts":   # Drafts are local-only (never on IMAP) — skip the round-trip
        moved = False
        try:   # prefer MOVE to Trash (recoverable) over a permanent expunge
            meta = await _asyncio.to_thread(_list_special_folders, current_user.id, db, acc.email)
            trash = meta.get("trash")
            if trash and trash != folder:
                moved = await _asyncio.to_thread(move_message, current_user.id, db, acc.email, uid, folder, trash)
        except Exception as e:
            logger.debug("[mail] trash move failed (%s): %s", uid, e)
        if not moved:   # no Trash folder / already in Trash → fall back to expunge
            try:
                await _asyncio.to_thread(delete_message, current_user.id, db, acc.email, uid, folder)
            except Exception as e:
                logger.debug("[mail] IMAP delete failed (%s): %s", uid, e)
    await mail_store.delete_message(_seckey(db, current_user), acc.email, folder, uid)
    return {"ok": True}


@router.post("/archive")
async def mail_archive(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    folder, uid = d.get("folder", "INBOX"), d.get("uid")
    try:
        await _asyncio.to_thread(archive_message, current_user.id, db, acc.email, uid, folder)
    except Exception as e:
        logger.debug("[mail] IMAP archive failed (%s): %s", uid, e)
    await mail_store.delete_message(_seckey(db, current_user), acc.email, folder, uid)
    return {"ok": True}


@router.post("/move")
async def mail_move(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Move a message to a folder the user picked (the Move control, which Archive is one entry of).

    Reuses `mail_service.move_message`, which already existed for "delete → Trash rather than
    expunge" — COPY, then \\Deleted, then EXPUNGE, in that order, so a failed copy cannot lose the
    message. Archive keeps its OWN endpoint because it may CREATE its destination, which is what
    makes one press work on an account that has never had an Archive folder — and is exactly what a
    user-chosen destination must not do.

    The local encrypted mirror of the SOURCE is dropped either way: the message is not there any
    more, and leaving it would show a copy no server has.
    """
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    folder, uid, dest = d.get("folder", "INBOX"), d.get("uid"), (d.get("dest") or "").strip()
    if not uid or not dest:
        raise HTTPException(status_code=400, detail="uid and dest are required")
    if dest == folder:
        return {"ok": True, "moved": False}
    ok = False
    try:
        ok = await _asyncio.to_thread(move_message, current_user.id, db, acc.email, uid, folder, dest)
    except Exception as e:
        logger.debug("[mail] IMAP move failed (%s -> %s): %s", uid, dest, e)
    if not ok:
        # Said out loud rather than swallowed: a move that silently did nothing leaves the message
        # where it was while the list has already removed it.
        raise HTTPException(status_code=502, detail="the server would not move that message")
    await mail_store.delete_message(_seckey(db, current_user), acc.email, folder, uid)
    return {"ok": True, "moved": True}


def _decode_attachments(items) -> list:
    """Compose attachments arrive as [{name,type,b64}] → (filename, bytes, content_type) for SMTP."""
    out = []
    for a in (items or []):
        try:
            out.append((a.get("name") or "attachment",
                        base64.b64decode(a.get("b64") or ""),
                        a.get("type") or "application/octet-stream"))
        except Exception:
            pass
    return out


@router.post("/send")
async def mail_send(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if not (d.get("to") or "").strip():
        return JSONResponse({"ok": False, "error": "Recipient required"}, status_code=400)
    ok = await _asyncio.to_thread(
        send_email, acc, d.get("to", ""), d.get("subject", ""), d.get("body", ""),
        d.get("html_body"), _decode_attachments(d.get("attachments")), None,
        d.get("cc", ""), d.get("bcc", ""))
    return {"ok": bool(ok)}


@router.post("/reply")
async def mail_reply(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    ok = await _asyncio.to_thread(
        reply_to_message, current_user.id, db, acc.email, d.get("uid"), d.get("body", ""),
        bool(d.get("reply_all")), _decode_attachments(d.get("attachments")), d.get("folder", "INBOX"))
    return {"ok": bool(ok)}


@router.post("/forward")
async def mail_forward(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if not (d.get("to") or "").strip():
        return JSONResponse({"ok": False, "error": "Recipient required"}, status_code=400)
    ok = await _asyncio.to_thread(
        forward_message, current_user.id, db, acc.email, d.get("uid"), d.get("to", ""),
        d.get("body", ""), _decode_attachments(d.get("attachments")), d.get("folder", "INBOX"))
    return {"ok": bool(ok)}


@router.get("/dl/{account_hint}/{folder}/{uid}/{idx}")
async def mail_download(account_hint: str, folder: str, uid: str, idx: int,
                        db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Download a received attachment: fetch the ciphertext from Blossom and decrypt it (the message
    doc holds the per-file key+iv). Inline for viewable types, attachment otherwise."""
    acc = _resolve_account(db, current_user, account_hint)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    msg = await mail_store.get_message(_seckey(db, current_user), acc.email, folder, uid)
    atts = (msg or {}).get("attachments") or []
    if idx < 0 or idx >= len(atts):
        raise HTTPException(status_code=404, detail="Attachment not found")
    ref = atts[idx]
    data, mime = await mail_sync.load_attachment(db, ref)
    if data is None:
        raise HTTPException(status_code=404, detail="Attachment unavailable")
    name = sanitize_filename(ref.get("name") or "attachment")
    disp = "inline" if any(t in mime for t in ("image/", "application/pdf", "text/")) else "attachment"
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": f'{disp}; filename="{name}"'})


@router.post("/draft")
async def mail_save_draft(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Save a compose draft as an encrypted Nostr doc in the virtual 'Drafts' folder. Overwrites the
    same uid when re-saving; the GUI lists/opens it like any folder, and a successful send deletes it."""
    import time
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    dr = d.get("draft") or {}
    uid = str(dr.get("uid") or f"draft-{int(time.time() * 1000)}")
    body = dr.get("body", "")
    doc = {
        "uid": uid, "is_draft": True, "from": acc.email,
        "to": dr.get("to", ""), "cc": dr.get("cc", ""),
        "subject": dr.get("subject", "") or "(no subject)",
        "body_text": body, "preview": (body or "")[:200],
        "attachments": dr.get("attachments") or [],
        "mode": dr.get("mode"), "reply_uid": dr.get("reply_uid"), "reply_folder": dr.get("reply_folder"),
        "flags": {"read": True}, "ts": int(time.time()),
    }
    sk = _seckey(db, current_user)
    doc = await mail_sync.offload_body(db, _ns.derive_pubkey(sk), doc)   # large draft body → encrypted blob
    ok = await mail_store.store_message(sk, acc.email, "Drafts", doc)
    return {"ok": bool(ok), "uid": uid}


@router.get("/folder-map")
async def mail_folder_map(account: str = "", db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """What this account's folders are, and which one currently plays each role.

    `detected` is what the server reports (RFC 6154 special-use, then name heuristics); `mapping` is
    the user's own override. The UI shows the effective choice and can tell the two apart.
    """
    acc = _resolve_account(db, current_user, account)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    meta = await _asyncio.to_thread(_list_special_folders, current_user.id, db, acc.email)
    saved = (mail_service.get_folder_map(current_user.id, db) or {}).get(acc.email, {})
    return {"account": acc.email, "folders": meta.get("all") or [],
            "detected": {k: meta.get(k) for k in ("sent", "drafts", "trash", "junk", "archive")},
            "mapping": saved}


@router.put("/folder-map")
async def mail_folder_map_save(request: Request, db: Session = Depends(get_db),
                               current_user: User = Depends(get_current_user)):
    """Save the mapping, then mirror it to the relay so it survives this node."""
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    full = mail_service.set_folder_map(current_user.id, db, acc.email, d.get("mapping") or {})
    # Persist off-box in the same breath. Waiting for some later sync would mean a mapping that
    # looks saved and is gone after a restore — the whole point of putting user kv on the relay.
    try:
        from app.services import users_store
        await users_store.sync_user_kv(db, current_user)
    except Exception as e:
        logger.warning("[mail] folder map saved locally but not mirrored: %s", e)
    return {"ok": True, "mapping": full.get(acc.email, {})}


@router.get("/folders")
async def mail_folders(account: str = "", db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Account's REAL folders with friendly labels + special-use mapping (so 'Sent' points at the
    server's actual sent mailbox, e.g. INBOX.Sent / [Gmail]/Sent Mail). Returns ordered names + a
    {name: label} map. 'Drafts' is the local compose-drafts folder, pinned separately."""
    acc = _resolve_account(db, current_user, account)
    if not acc:
        return {"folders": ["INBOX", "Drafts"], "labels": {"INBOX": "📥 Inbox", "Drafts": "📝 Drafts"}}
    try:
        meta = await _asyncio.to_thread(_list_special_folders, current_user.id, db, acc.email)
    except Exception as e:
        logger.debug("[mail] list_special_folders failed: %s", e)
        meta = {"all": ["INBOX"]}
    allf = meta.get("all") or ["INBOX"]
    sent, trash, junk, archive = meta.get("sent"), meta.get("trash"), meta.get("junk"), meta.get("archive")
    inbox = next((f for f in allf if f.upper() == "INBOX"), "INBOX")

    def label(n):
        if n == sent: return "📤 Sent"
        if n == trash: return "🗑 Trash"
        if n == junk: return "⚠️ Spam"
        if n == archive: return "🗄 Archive"
        if n.upper() == "INBOX": return "📥 Inbox"
        return "📁 " + (n.replace("INBOX.", "").replace("[Gmail]/", "").split("/")[-1] or n)

    order = [inbox]
    if sent and sent != inbox:
        order.append(sent)
    order.append("Drafts")   # local compose-drafts (virtual)
    for f in allf:
        if f not in order and f != inbox:
            order.append(f)
    labels = {"Drafts": "📝 Drafts"}
    for f in order:
        if f != "Drafts":
            labels[f] = label(f)
    return {"folders": order, "labels": labels, "sent": sent}


@router.post("/sync-folder")
async def mail_sync_folder(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Pull one folder on demand (the default sync only does INBOX/Sent)."""
    d = await request.json()
    if d.get("account") == "__all":
        # A logical folder in the unified view spans every account and may have a different real
        # IMAP name in each. sync_one resolves the role per account; do not quietly refresh INBOX
        # when the person clicked unified Sent.
        out = {}
        try:
            for item in (get_user_mail_accounts(current_user.id, db) or []):
                out[item.email] = await mail_sync.sync_one(
                    db, current_user, item.email, d.get("folder", "INBOX"))
            return {"ok": True, "new": out}
        except Exception as e:
            logger.warning("[mail] unified sync-folder failed: %s", e)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        n = await mail_sync.sync_one(db, current_user, acc.email, d.get("folder", "INBOX"))
        return {"ok": True, "new": n}
    except Exception as e:
        logger.warning("[mail] sync-folder failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


import re as _re


def _normsubj(s: str) -> str:
    """The subject with every reply/forward prefix stripped, not just the first.

    Mail acquires them by accretion — "Re: Re: Fwd: quote" is ordinary after a few round trips, and
    a single non-repeating strip left "re: fwd: quote", which matches nothing. Bounded rather than
    greedy-anchored so a pathological subject cannot spin."""
    # One anchored, repeating group rather than a loop with a cap: a cap silently stops matching
    # past its depth, and every alternative must consume a prefix, so this cannot backtrack badly.
    return _re.sub(r'^(?:\s*(?:re|fwd|fw)\s*:\s*)+', '', (s or '').strip(),
                   flags=_re.I).strip().lower()


def _idset(m: dict) -> set:
    s = set()
    for k in ("message_id", "in_reply_to"):
        v = (m.get(k) or "").strip()
        if v:
            s.add(v)
    for r in (m.get("references") or "").split():
        r = r.strip()
        if r:
            s.add(r)
    return s


def _is_reply(m: dict) -> bool:
    """Does this message CLAIM to continue an existing conversation?

    A message carrying `In-Reply-To` or `References` is a reply whose parent we may simply not hold
    — a mailing list stripped the headers, or the parent is in a folder we did not read. Matching it
    to a same-subject message is a repair.

    A message with NEITHER is a ROOT, and two roots are never the same conversation however identical
    their subjects. That is the difference the subject fallback did not draw: it grouped anything
    sharing a subject, so four separate "Kraken" notices — each a root, each its own event, sent days
    apart — arrived as one thread. Reported as "it grouped 4 different kraken emails into 1 thread /
    it should be by thread/conversation".

    Automated senders are exactly the case that breaks: they reuse one subject for months and never
    reference anything, so subject alone says nothing about whether two of their messages belong
    together. Header links still do all the real work above; this only bounds the fallback.
    """
    if (m.get("in_reply_to") or "").strip():
        return True
    return bool((m.get("references") or "").strip())


def _looks_sent(name: str) -> bool:
    """Does this folder name MEAN sent?

    `_logical_of` only rewrites a folder when the server's RFC 6154 special-use flags name it, and
    returns the raw name otherwise — so a real mailbox stored 52 messages under `Sent Messages` and
    39 under `Sent`, and an equality check against "sent" found 39 of 91. Same story for
    `Deleted Messages` beside `Trash`.

    Matched on WHOLE words so `Sent Messages`, `Sent Items`, `INBOX.Sent` and `[Gmail]/Sent Mail`
    all qualify while a folder called `Consent forms` does not."""
    return "sent" in _re.split(r"[^a-z]+", str(name or "").lower())


def _is_own_sent(m: dict) -> bool:
    """Is this the user's OWN copy of something they sent?

    The subject fallback is bounded to replies because two ROOTS are never one conversation — that
    is what stopped four separate Kraken notices arriving as one thread. But it also excluded the
    one root that genuinely does belong: your own outgoing message.

    Outgoing mail carried no `Message-ID` until it was added in mail_service.send_email, so every
    message sent before that has none and never will: the copy in Sent cannot be linked by headers
    to the reply it produced, in either direction. Header threading handles everything sent from now
    on; this is what rescues the history.

    Narrow on purpose. A message in the user's own Sent folder sharing a normalised subject is their
    side of that conversation. An inbound root from an automated sender sharing a subject with
    another inbound root is not, and still is not admitted."""
    return _looks_sent(m.get("logical")) or _looks_sent(m.get("folder"))


def _graph_isolated(allmsgs: list):
    """A predicate: can the reference graph reach this message, in either direction?

    "HAS NO MESSAGE-ID" WAS THE WRONG QUESTION, and it is the one the additive own-sent pass used
    to ask. A Message-ID is not evidence about which conversation a message belongs to — evidence is
    an `In-Reply-To`/`References` that resolves to a message we hold, or an ID that some message we
    hold points AT. Plenty of this app's own outgoing mail carries an ID that nothing ever
    referenced and references nothing itself: the graph can neither reach it nor be reached from it,
    so a subject match adds information rather than overriding anything, exactly as it does for a
    message with no ID at all.

    Measured on the reporting mailbox (17,921 messages, 907 of them the user's own sent mail):
    622 sent messages are graph-isolated, of which 441 share a normalised subject with a message
    that is not theirs. The old ID test admitted only the 341 with no ID, leaving 100 outgoing
    messages out of conversations they demonstrably belong to. Measured per ACCOUNT, which is how
    threading actually runs, that is +11 sent messages reachable (369 → 380).

    The two sets are computed once per call and closed over: `_build_thread` runs this over the
    whole cached mailbox scan, so a per-message rescan would be O(n²)."""
    held, referenced = set(), set()
    for m in allmsgs:
        mid = (m.get("message_id") or "").strip()
        if mid:
            held.add(mid)
        irt = (m.get("in_reply_to") or "").strip()
        if irt:
            referenced.add(irt)
        for r in (m.get("references") or "").split():
            r = r.strip()
            if r:
                referenced.add(r)

    def isolated(m: dict) -> bool:
        mid = (m.get("message_id") or "").strip()
        if mid and mid in referenced:          # somebody replied to it — the graph reaches it
            return False
        for x in [(m.get("in_reply_to") or "").strip()] + (m.get("references") or "").split():
            x = x.strip()
            if x and x in held:                # it points at something we hold — it reaches the graph
                return False
        return True

    return isolated


def _build_thread(seed: dict, allmsgs: list) -> list:
    """Group a conversation: close the Message-ID/References/In-Reply-To reference graph, with a
    normalized-subject fallback when there are no usable headers. Cross-folder. Oldest→newest."""
    related = _idset(seed)
    members = {seed.get("message_id") or f"uid:{seed.get('folder')}:{seed.get('uid')}": seed}
    while True:
        added = False
        for m in allmsgs:
            key = m.get("message_id") or f"uid:{m.get('folder')}:{m.get('uid')}"
            if key in members:
                continue
            if _idset(m) & related:
                members[key] = m
                related |= _idset(m)
                added = True
        if not added:
            break
    msgs = list(members.values())
    if len(msgs) <= 1:                                  # no header links → fall back to subject
        ns = _normsubj(seed.get("subject", ""))
        if ns:
            by = {}
            for m in allmsgs:
                if _normsubj(m.get("subject", "")) == ns and (_is_reply(m) or _is_own_sent(m)):
                    by[(m.get("folder"), m.get("uid"))] = m
            by[(seed.get("folder"), seed.get("uid"))] = seed
            msgs = list(by.values())
    # ── OWN SENT MAIL THAT NO HEADER CAN REACH ──────────────────────────────────────────────────
    #
    # Reported four times, most plainly as "you didn't fix emails conversations mode showing sent
    # items in thread". The threading rules were right and the scan was complete; the DATA is what
    # is missing. Measured on the reporting mailbox: 907 sent messages, of which only 503 carry a
    # Message-ID at all. This app did not set one on outgoing mail until it was fixed, so 404 of the
    # user's own replies have no identity, are referenced by nothing, and reference nothing — they
    # cannot be linked by the graph above however good it is, and the subject fallback below could
    # never reach them either because it only runs when the graph found NOTHING.
    #
    # So this is additive and deliberately narrow: a message is pulled in only when it is the user's
    # OWN SENT mail, carries no Message-ID (so it is unthreadable by any other means and nothing is
    # being second-guessed), and shares the seed's normalized subject. Anything with an ID keeps
    # being threaded by headers alone — a subject match is a guess, and a guess must never override
    # evidence.
    ns_own = _normsubj(seed.get("subject", ""))
    if ns_own:
        isolated = _graph_isolated(allmsgs)
        held = {(m.get("folder"), m.get("uid")) for m in msgs}
        for m in allmsgs:
            if (m.get("folder"), m.get("uid")) in held:
                continue
            if not _is_own_sent(m) or not isolated(m):
                continue
            if _normsubj(m.get("subject", "")) == ns_own:
                msgs.append(m)
                held.add((m.get("folder"), m.get("uid")))

    msgs.sort(key=lambda m: m.get("ts", 0))
    return msgs


_THREAD_SCAN: dict = {}
_THREAD_SCAN_TTL = 60.0


async def _thread_scan(sk: bytes, account_email: str | None, user_id) -> list:
    """The whole mailbox for threading, cached briefly per (user, account).

    KEYED ON THE USER'S ID, NEVER `id(sk)` — AND THAT ONE WORD IS WHY THE CACHE NEVER WORKED.
    `_seckey` ends in `bytes.fromhex(...)`, which mints a NEW object on every call, so `id(sk)` was
    a different integer for every request: measured back to back, 139845150742352 then
    139845082027296. The lookup therefore missed every single time, and each miss is a full paged
    walk of the mailbox — measured at 17,921 documents in 13.6 seconds of NIP-44 decrypts, on the
    single uvicorn worker, for EVERY message opened. That is what "the conversation doesn't load"
    feels like from the outside: the thread arrives thirteen seconds after the message, or the user
    has moved on before it does.

    The recycled-address hazard went with it: CPython reuses a freed object's id, so two users
    reading the unified mailbox (`account_email is None`) could in principle have collided on one
    cache entry. A user id cannot be recycled."""
    import time as _time
    key = (user_id, account_email or "*")
    hit = _THREAD_SCAN.get(key)
    now = _time.monotonic()
    if hit and now - hit[0] < _THREAD_SCAN_TTL:
        return hit[1]
    msgs = await mail_store.list_all_messages(sk, account_email, None)
    # Bounded: one entry per account, and stale ones are dropped rather than accumulated.
    for k, v in list(_THREAD_SCAN.items()):
        if now - v[0] >= _THREAD_SCAN_TTL:
            _THREAD_SCAN.pop(k, None)
    _THREAD_SCAN[key] = (now, msgs)
    return msgs


@router.get("/thread")
async def mail_thread(account: str, uid: str, folder: str = "INBOX",
                      db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The whole conversation for a message (across folders), bodies rehydrated."""
    sk = _seckey(db, current_user)
    acc = None
    # An ABSENT account means the whole mailbox, however the client spelled it. `this.acct` is null
    # in the All-inboxes view and `encodeURIComponent(null)` is the four characters n-u-l-l, which
    # resolved to no account and 404'd the conversation — silently, because the client catches. The
    # message still opened, so nothing looked broken; the thread simply never arrived. There is no
    # such thing as an account named "null", so nothing legitimate is reinterpreted here.
    if account in ("null", "undefined", "", None):
        account = "__all"
    allm = None
    if account == "__all":
        # THE UNIFIED SEED CAME OUT OF ONE 5000-DOCUMENT PAGE, AND THAT IS A 404 FOR MOST OF A REAL
        # MAILBOX. `list_messages(..., limit=0)` reads a single page and the relay clamps any filter
        # to 5000, newest-first by the time the document was WRITTEN. Measured on the reporting
        # mailbox (17,921 documents, four accounts, so the client opens on All inboxes): that page
        # held 3,161 Trash and 1,711 Deleted Messages, and of the mail somebody actually reads it
        # could find 32 of 39 in INBOX and **3 of 2,717 in INBOX.Archive**. For the other 2,714 this
        # endpoint answered "Message not found", the client logged it to the console and the
        # conversation never upgraded past the one message that was clicked — indistinguishable from
        # threading that simply found nothing.
        #
        # The paged scan is the same one the thread builder needs anyway, so finding the seed in it
        # costs nothing extra and cannot be short.
        allm = await _thread_scan(sk, None, current_user.id)
        seed = next((m for m in allm
                     if str(m.get("uid")) == str(uid) and m.get("folder") == folder), None)
    else:
        acc = _resolve_account(db, current_user, account)
        if not acc:
            raise HTTPException(status_code=404, detail="Account not found")
        seed = await mail_store.get_message(sk, acc.email, folder, uid)
    if not seed:
        raise HTTPException(status_code=404, detail="Message not found")
    # A root message has no In-Reply-To/References of its own, but later replies point at its
    # Message-ID. Treating that root as a singleton made the same conversation appear complete when
    # opened from a reply and incomplete when opened from the first message. A usable Message-ID is
    # therefore enough to search the reference graph.
    #
    # AND A MESSAGE WITH NO HEADERS AT ALL IS NOT A SINGLETON EITHER — IT IS THE ONE THAT NEEDS THE
    # SCAN MOST. The short circuit here used to end the request for any seed carrying no
    # Message-ID, In-Reply-To or References, which is exactly the shape this app's own older
    # outgoing mail has: opening one of your own sent messages could only ever show that message,
    # for ever, however complete the conversation around it was. Measured on the reporting mailbox,
    # per account: 393 sent seeds and 478 inbound seeds take this branch, and the subject fallback
    # would give 319 of the sent ones and 174 of the inbound ones a real conversation — 493
    # messages that could not show one at all.
    #
    # It was a cost guard, and the cost it was guarding against was a bug: `_thread_scan` keyed its
    # cache on `id(sk)`, so it missed every time and every open paid 13.6 seconds. With the cache
    # actually hitting, this is one scan per account per minute. The only seed still short-circuited
    # is one with nothing whatsoever to match on — no headers AND no subject once the Re:/Fwd:
    # prefixes come off — where the scan could not find anything however long it ran.
    if not (seed.get("message_id") or seed.get("in_reply_to") or
            (seed.get("references") or "").strip() or _normsubj(seed.get("subject", ""))):
        thread = [seed]
    else:
        # PAGED, AND CACHED FOR A MINUTE.
        #
        # `list_messages(..., limit=0)` is ONE page and the relay clamps any filter to 5000, so on a
        # real mailbox "all messages" quietly meant "the newest 5000 documents". Measured on the
        # mailbox this was reported from: 5,000 of 17,903 documents, and 91 of 907 sent messages —
        # the thread builder could not see 90% of the user's own sent mail however good the
        # threading rules were. Reported as "Email is missing messages I sent in the thread".
        #
        # Walking every page costs ~10s of NIP-44 decrypts there, which is why it is cached: the
        # client renders the message immediately and upgrades when this returns, so the READ is
        # never blocked by it, and opening several messages in a row pays for one scan.
        if allm is None:
            allm = await _thread_scan(sk, acc.email if acc else None, current_user.id)
        thread = _build_thread(seed, allm)
    for m in thread:                                    # rehydrate offloaded bodies (bounded by thread size)
        if m.get("body_ref"):
            body = await mail_sync.load_body(db, m["body_ref"])
            if body:
                m["body_text"] = body.get("body_text", "")
                m["body_html"] = body.get("body_html", "")
    return {"messages": thread}
