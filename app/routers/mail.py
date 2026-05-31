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
from app.services.mail_service import get_attachment, get_user_mail_accounts, sanitize_filename
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
