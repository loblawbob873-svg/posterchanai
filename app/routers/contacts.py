"""
Contacts Router - API endpoints for CardDAV contacts management.
"""
from fastapi import APIRouter, Depends, Request, HTTPException, Form
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session
from typing import List
from pathlib import Path
from datetime import datetime
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


@router.get("/export")
async def export_contacts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export all contacts as a single vCard (.vcf) file."""
    try:
        from app.services.cardav_server import get_user_cardav_path
        import vobject
        
        # Get user's CardDAV path
        cardav_path = get_user_cardav_path(current_user, db)
        
        if not cardav_path.exists():
            raise HTTPException(status_code=404, detail="CardDAV directory not found")
        
        # Read all .vcf files and combine them
        combined_vcards = []
        contact_count = 0
        
        for vcf_file in cardav_path.glob("*.vcf"):
            try:
                with open(vcf_file, 'r', encoding='utf-8') as f:
                    vcard_data = f.read()
                
                # Validate it's a valid vCard
                vcard = vobject.readOne(vcard_data)
                combined_vcards.append(vcard_data)
                contact_count += 1
            except Exception as e:
                logger.warning(f"Error reading {vcf_file}: {e}")
                continue
        
        if contact_count == 0:
            raise HTTPException(status_code=404, detail="No contacts found to export")
        
        # Combine all vCards into a single file (separated by blank lines)
        vcf_content = "\n".join(combined_vcards)
        
        # Return as downloadable file
        return Response(
            content=vcf_content,
            media_type="text/vcard; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="contacts_{current_user.username}_{datetime.utcnow().strftime("%Y%m%d")}.vcf"'
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting contacts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to export contacts: {str(e)}")


@router.post("/import")
async def import_contacts(
    vcf_data: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Import contacts from vCard (.vcf) data."""
    try:
        from app.services.cardav_server import get_user_cardav_path
        import vobject
        import uuid
        
        # Get user's CardDAV path
        cardav_path = get_user_cardav_path(current_user, db)
        cardav_path.mkdir(parents=True, exist_ok=True)
        
        imported_count = 0
        error_count = 0
        skipped_count = 0
        
        # Parse vCard data (may contain multiple vCards)
        try:
            # Try parsing as multiple vCards
            vcards = []
            for vcard in vobject.readComponents(vcf_data):
                if vcard.name == 'VCARD':
                    vcards.append(vcard)
        except Exception:
            # Try parsing as single vCard
            try:
                vcard = vobject.readOne(vcf_data)
                if vcard.name == 'VCARD':
                    vcards = [vcard]
                else:
                    vcards = []
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid vCard data: {str(e)}")
        
        if not vcards:
            raise HTTPException(status_code=400, detail="No valid vCards found in the data")
        
        # Import each vCard
        for vcard in vcards:
            try:
                # Get or generate UID
                if hasattr(vcard, 'uid'):
                    contact_uid = str(vcard.uid.value)
                else:
                    contact_uid = str(uuid.uuid4())
                    vcard.add('uid')
                    vcard.uid.value = contact_uid
                
                # Check if contact already exists
                vcf_file = cardav_path / f"{contact_uid}.vcf"
                if vcf_file.exists():
                    logger.debug(f"Contact {contact_uid} already exists, skipping")
                    skipped_count += 1
                    continue
                
                # Save vCard to file
                vcard_data = vcard.serialize()
                with open(vcf_file, 'w', encoding='utf-8') as f:
                    f.write(vcard_data)
                
                imported_count += 1
            except Exception as e:
                logger.warning(f"Error importing vCard: {e}")
                error_count += 1
                continue
        
        return {
            "success": True,
            "message": f"Imported {imported_count} contacts",
            "imported": imported_count,
            "skipped": skipped_count,
            "errors": error_count
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error importing contacts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to import contacts: {str(e)}")


@router.post("/import/cardav")
async def import_from_cardav_server(
    cardav_url: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Import contacts from another CardDAV server."""
    try:
        import caldav
        from app.services.cardav_server import get_user_cardav_path
        import vobject
        import uuid
        
        # Get user's CardDAV path
        cardav_path = get_user_cardav_path(current_user, db)
        cardav_path.mkdir(parents=True, exist_ok=True)
        
        # Connect to CardDAV server
        client = caldav.DAVClient(
            url=cardav_url.rstrip('/'),
            username=username,
            password=password
        )
        
        # Get user's principal
        principal = client.principal()
        
        # Get address books
        address_books = principal.address_books()
        
        imported_count = 0
        error_count = 0
        skipped_count = 0
        
        # Import contacts from each address book
        for address_book in address_books:
            try:
                # Fetch all contacts (vCards) from this address book
                contacts = address_book.objects()
                
                for contact in contacts:
                    try:
                        # Get the vCard data
                        vcard_data = contact.data
                        
                        # Parse vCard
                        vcard = vobject.readOne(vcard_data)
                        
                        # Get or generate UID
                        if hasattr(vcard, 'uid'):
                            contact_uid = str(vcard.uid.value)
                        else:
                            contact_uid = str(uuid.uuid4())
                            vcard.add('uid')
                            vcard.uid.value = contact_uid
                        
                        # Check if contact already exists
                        vcf_file = cardav_path / f"{contact_uid}.vcf"
                        if vcf_file.exists():
                            logger.debug(f"Contact {contact_uid} already exists, skipping")
                            skipped_count += 1
                            continue
                        
                        # Save to user's CardDAV directory
                        with open(vcf_file, 'w', encoding='utf-8') as f:
                            f.write(vcard_data if isinstance(vcard_data, str) else vcard_data.decode('utf-8'))
                        
                        imported_count += 1
                    except Exception as e:
                        logger.warning(f"Error importing contact: {e}")
                        error_count += 1
                        continue
            except Exception as e:
                logger.warning(f"Error importing from address book {address_book.name}: {e}")
                error_count += 1
                continue
        
        return {
            "success": True,
            "message": f"Imported {imported_count} contacts from CardDAV server",
            "imported": imported_count,
            "skipped": skipped_count,
            "errors": error_count
        }
    except ImportError:
        raise HTTPException(status_code=500, detail="caldav library not installed. Install with: pip install caldav")
    except Exception as e:
        logger.error(f"Error importing from CardDAV server: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to import from CardDAV server: {str(e)}")
