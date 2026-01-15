"""
Contacts Router - API endpoints for CardDAV contacts management.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List
import logging
import traceback

from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services.caldav_service import (
    get_user_contacts,
    get_user_contacts_config,
    get_user_contact_by_uid,
    edit_user_contact,
)

router = APIRouter(prefix="/api/contacts", tags=["contacts"])
logger = logging.getLogger(__name__)


@router.get("/emails")
async def get_contact_emails(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> List[dict]:
    """Get contact email addresses for autocomplete."""
    config = get_user_contacts_config(current_user.id, db)
    if not config:
        return []

    contacts = get_user_contacts(current_user.id, "", db)

    emails = []
    seen = set()
    for contact in contacts:
        for email in contact.emails:
            if email and email not in seen:
                seen.add(email)
                emails.append({
                    "email": email,
                    "name": contact.name or email.split("@")[0]
                })

    emails.sort(key=lambda x: x["name"].lower())
    return emails


@router.get("/{uid}")
async def get_contact(
    uid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> dict:
    """Get a single contact by UID."""
    config = get_user_contacts_config(current_user.id, db)
    if not config:
        return JSONResponse({"error": "CardDAV not configured"}, status_code=404)

    contact = get_user_contact_by_uid(current_user.id, db, uid)
    if not contact:
        return JSONResponse({"error": "Contact not found"}, status_code=404)

    return {
        "uid": contact.uid,
        "name": contact.name,
        "phone": contact.phone,
        "email": contact.emails[0] if contact.emails else "",
        "emails": contact.emails,
        "organization": contact.organization,
        "note": contact.note,
    }


@router.put("/{uid}")
async def update_contact(
    uid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a contact by UID."""
    try:
        body = await request.json()
        logger.info(f"Update contact request: uid={uid}, body={body}")
        
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
