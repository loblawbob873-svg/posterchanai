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
                                       archive_message, delete_message)
from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mail", tags=["mail"])


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
from app.services import mail_store, mail_sync, nostr_store
from app.services.nostr import nostr_service as _ns


def _seckey(db, user):
    return nostr_store.user_storage_seckey(db, user)


def _resolve_account(db, user, hint: str):
    """Match an account by substring (or the first account when hint is blank)."""
    accts = get_user_mail_accounts(user.id, db)
    if not hint:
        return accts[0] if accts else None
    for a in accts:
        if hint.lower() in a.email.lower():
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
    """Login-triggered + manual refresh: IMAP → encrypted mailbox. Returns new-message counts."""
    try:
        res = await mail_sync.sync_all(db, current_user)
        return {"ok": True, "new": res}
    except Exception as e:
        logger.warning("[mail] sync failed for %s: %s", current_user.id, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/messages")
async def mail_messages(account: str = "", folder: str = "INBOX",
                        db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    sk = _seckey(db, current_user)
    if account == "__all":   # unified view: this folder across EVERY account (each summary carries its own account)
        msgs = [m for m in await mail_store.list_messages(sk, None, None) if m.get("folder") == folder]
        return {"messages": [_summary(m) for m in msgs], "account": "__all"}
    acc = _resolve_account(db, current_user, account)
    if not acc:
        return {"messages": [], "account": None}
    msgs = await mail_store.list_messages(sk, acc.email, folder)
    return {"messages": [_summary(m) for m in msgs], "account": acc.email}


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
    if not (msg.get("flags") or {}).get("read"):     # opening marks it read
        await mail_store.set_flags(sk, acc.email, folder, uid, read=True)
        msg.setdefault("flags", {})["read"] = True
    return {"message": msg}


@router.get("/search")
async def mail_search(q: str, account: str = "", folder: str = "",
                      db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    sk = _seckey(db, current_user)
    if account == "__all":
        msgs = await mail_store.list_messages(sk, None, None)
    else:
        acc = _resolve_account(db, current_user, account)
        msgs = await mail_store.list_messages(sk, acc.email if acc else None, folder or None)
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
        try:   # IMAP delete is best-effort; the mailbox doc removal is what the GUI reflects
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


from app.services.mail_service import list_folders as _list_folders


@router.get("/folders")
async def mail_folders(account: str = "", db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Real IMAP folder list for an account (INBOX/Sent/Drafts pinned first), so the GUI shows the
    account's actual mailboxes rather than a fixed three."""
    acc = _resolve_account(db, current_user, account)
    if not acc:
        return {"folders": ["INBOX", "Sent", "Drafts"]}
    try:
        raw = await _asyncio.to_thread(_list_folders, current_user.id, db, acc.email)
    except Exception as e:
        logger.debug("[mail] list_folders failed: %s", e)
        raw = []
    pinned = ["INBOX", "Sent", "Drafts"]
    rest = sorted({f for f in (raw or []) if f and f not in pinned})
    return {"folders": pinned + rest}


@router.post("/sync-folder")
async def mail_sync_folder(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Pull one folder on demand (the default sync only does INBOX/Sent)."""
    d = await request.json()
    acc = _resolve_account(db, current_user, d.get("account", ""))
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        n = await mail_sync.sync_one(db, current_user, acc.email, d.get("folder", "INBOX"))
        return {"ok": True, "new": n}
    except Exception as e:
        logger.warning("[mail] sync-folder failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
