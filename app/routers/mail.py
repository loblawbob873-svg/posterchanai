"""
Mail Router - API endpoints for email functionality.
"""
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
import logging
import traceback

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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a contact by UID."""
    logger = logging.getLogger(__name__)
    
    try:
        # Parse JSON body manually
        body = await request.json()
        logger.info(f"Update contact request: uid={uid}, body={body}")
        
        # Filter to allowed fields
        allowed_fields = {"name", "phone", "email", "organization", "note"}
        updates_dict = {k: v for k, v in body.items() if k in allowed_fields and v is not None}
        
        if not updates_dict:
            return JSONResponse({"success": False, "error": "No valid fields to update"})
        
        config = get_user_contacts_config(current_user.id, db)
        if not config:
            return JSONResponse({"success": False, "error": "CardDAV not configured"})

        contact = get_user_contact_by_uid(current_user.id, db, uid)
        if not contact:
            return JSONResponse({"success": False, "error": f"Contact not found: {uid}"})

        success = edit_user_contact(current_user.id, db, uid, updates_dict)
        if not success:
            return JSONResponse({"success": False, "error": "CardDAV server rejected the update"})

        return JSONResponse({"success": True, "message": "Contact updated"})
    
    except Exception as e:
        error_trace = traceback.format_exc()
        logger.error(f"Exception updating contact: {error_trace}")
        return JSONResponse({"success": False, "error": str(e), "trace": error_trace})


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
