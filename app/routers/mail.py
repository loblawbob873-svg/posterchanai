"""
Mail Router - API endpoints for email functionality.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services.mail_service import get_attachment, get_user_mail_accounts, sanitize_filename
from app.services.caldav_service import get_user_contacts, get_user_contacts_config

router = APIRouter(prefix="/api/mail", tags=["mail"])


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
        # Contact has single email field, not emails list
        email = contact.email
        if email and email not in seen:
            seen.add(email)
            emails.append({
                "email": email,
                "name": contact.name or email.split("@")[0]
            })

    # Sort by name
    emails.sort(key=lambda x: x["name"].lower())
    return emails


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
