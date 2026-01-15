"""
Mail Router - API endpoints for email functionality.
"""
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services.mail_service import get_attachment, get_user_mail_accounts, sanitize_filename
from app.services.caldav_service import (
    get_user_contacts,
    get_user_contacts_config,
    get_user_contact_by_uid,
    edit_user_contact,
)

router = APIRouter(prefix="/api/mail", tags=["mail"])


class ContactUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    organization: Optional[str] = None
    note: Optional[str] = None


@router.get("/contacts/emails")
async def get_contact_emails(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> List[dict]:
    """Get contact email addresses for autocomplete."""
    # Check if CardDAV is configured
    config = get_user_contacts_config(current_user.id, db)
    if not config:
        return []

    # Get all contacts (empty query returns all)
    contacts = get_user_contacts(current_user.id, "", db)

    # Extract unique emails with contact names
    emails = []
    seen = set()
    for contact in contacts:
        # Contact may have multiple emails
        for email in contact.emails:
            if email and email not in seen:
                seen.add(email)
                emails.append({
                    "email": email,
                    "name": contact.name or email.split("@")[0]
                })

    # Sort by name
    emails.sort(key=lambda x: x["name"].lower())
    return emails


@router.get("/contacts/{uid}")
async def get_contact(
    uid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> dict:
    """Get a single contact by UID."""
    config = get_user_contacts_config(current_user.id, db)
    if not config:
        raise HTTPException(status_code=404, detail="CardDAV not configured")

    contact = get_user_contact_by_uid(current_user.id, db, uid)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    return {
        "uid": contact.uid,
        "name": contact.name,
        "phone": contact.phone,
        "email": contact.emails[0] if contact.emails else "",
        "emails": contact.emails,
        "organization": contact.organization,
        "note": contact.note,
    }


@router.put("/contacts/{uid}")
async def update_contact(
    uid: str,
    updates: ContactUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> dict:
    """Update a contact by UID."""
    import logging
    logger = logging.getLogger(__name__)
    
    # Convert Pydantic model to dict, excluding None values
    # Use dict() for Pydantic v1 compatibility, model_dump() for v2
    try:
        updates_dict = updates.model_dump(exclude_none=True)
    except AttributeError:
        updates_dict = {k: v for k, v in updates.dict().items() if v is not None}
    logger.info(f"Update contact request: uid={uid}, updates={updates_dict}")
    
    config = get_user_contacts_config(current_user.id, db)
    if not config:
        logger.error("CardDAV not configured")
        raise HTTPException(status_code=404, detail="CardDAV not configured")

    # Validate that contact exists
    contact = get_user_contact_by_uid(current_user.id, db, uid)
    if not contact:
        logger.error(f"Contact not found: {uid}")
        raise HTTPException(status_code=404, detail="Contact not found")

    if not updates_dict:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    try:
        success = edit_user_contact(current_user.id, db, uid, updates_dict)
        if not success:
            logger.error(f"edit_user_contact returned False for uid={uid}")
            raise HTTPException(status_code=500, detail="Failed to update contact in CardDAV server")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Exception updating contact: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error updating contact: {str(e)}")

    return {"success": True, "message": "Contact updated"}


@router.get("/attachment/{account_hint}/{uid}/{index}")
async def download_attachment(
    account_hint: str,
    uid: str,
    index: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Download an email attachment."""
    # Find the full account email from hint
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

    # Use inline for viewable files (PDFs, images), attachment for others
    content_type = attachment.content_type.lower() if attachment.content_type else ''
    viewable_types = ['application/pdf', 'image/', 'text/plain', 'text/html']
    disposition = 'inline' if any(t in content_type for t in viewable_types) else 'attachment'

    # Sanitize filename to prevent header injection
    safe_filename = sanitize_filename(attachment.filename)

    return Response(
        content=attachment.data,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_filename}"'
        }
    )
