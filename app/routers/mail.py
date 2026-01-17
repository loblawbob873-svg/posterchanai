"""
Mail Router - API endpoints for email functionality.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, RedirectResponse, FileResponse
from sqlalchemy.orm import Session
from typing import List
from pathlib import Path
from urllib.parse import unquote

from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services.mail_service import get_attachment, get_user_mail_accounts, sanitize_filename
from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base

router = APIRouter(prefix="/api/mail", tags=["mail"])


@router.get("/contacts/emails")
async def get_contact_emails_redirect():
    """Redirect to new contacts API for backward compatibility."""
    return RedirectResponse(url="/api/contacts/emails", status_code=307)


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
    except:
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
    except:
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
    if not _validate_path_within_base(file_path, base_path):
        raise HTTPException(status_code=403, detail="Invalid file path")
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment not found")
    
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
