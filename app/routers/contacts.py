"""
Contacts Router - API endpoints for CardDAV contacts management.
"""
from fastapi import APIRouter, Depends, Request, HTTPException, Form, File, UploadFile
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
    """Export all contacts as a single vCard (.vcf) file. Uses storage proxy if configured."""
    try:
        from app.services.dav_storage_proxy import DAVStorageProxy
        import vobject
        
        # Use storage proxy (will fallback to local if not configured)
        proxy = DAVStorageProxy(db, current_user.username, 'cardav')
        
        # Read all .vcf files and combine them
        combined_vcards = []
        contact_count = 0
        
        # Get all .vcf files from root and subdirectories
        def collect_contacts(subpath: str = ""):
            """Recursively collect contacts from addressbook directories."""
            nonlocal contact_count
            items = proxy.list_files(subpath)
            
            for item in items:
                name = item.get('name', '')
                item_type = item.get('type', 'file')
                
                if item_type == 'directory':
                    # Recursively process subdirectories (addressbook subdirectories)
                    new_subpath = f"{subpath}/{name}" if subpath else name
                    collect_contacts(new_subpath)
                elif name.endswith('.vcf'):
                    # Read and process .vcf file
                    try:
                        filepath = f"{subpath}/{name}" if subpath else name
                        vcard_data = proxy.read_file(filepath)
                        
                        if vcard_data:
                            # Validate it's a valid vCard
                            vcard = vobject.readOne(vcard_data)
                            combined_vcards.append(vcard_data)
                            contact_count += 1
                    except Exception as e:
                        logger.warning(f"Error reading {filepath}: {e}")
                        continue
        
        # Start collecting from root
        collect_contacts()
        
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
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Import contacts from vCard (.vcf) file. Uses storage proxy if configured."""
    try:
        from app.services.dav_storage_proxy import DAVStorageProxy
        import vobject
        import uuid
        
        # Read uploaded file
        vcf_data = await file.read()
        vcf_data = vcf_data.decode('utf-8')
        
        # Use storage proxy (will fallback to local if not configured)
        proxy = DAVStorageProxy(db, current_user.username, 'cardav')
        
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
            raise HTTPException(status_code=400, detail="No valid vCards found in the file")
        
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
                
                # Build filepath
                filepath = f"{contact_uid}.vcf"
                
                # Check if contact already exists
                if proxy.file_exists(filepath):
                    logger.debug(f"Contact {contact_uid} already exists, skipping")
                    skipped_count += 1
                    continue
                
                # Save vCard to file using proxy
                vcard_data = vcard.serialize()
                success = proxy.write_file(filepath, vcard_data)
                
                if not success:
                    logger.warning(f"Failed to save contact {contact_uid}")
                    error_count += 1
                    continue
                
                imported_count += 1
            except Exception as e:
                logger.warning(f"Error importing vCard: {e}")
                error_count += 1
                continue
        
        return {
            "success": True,
            "count": imported_count,
            "message": f"Imported {imported_count} contact(s), skipped {skipped_count}, errors {error_count}"
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
    """Import contacts from another CardDAV server. Uses storage proxy if configured."""
    try:
        import caldav
        from app.services.dav_storage_proxy import DAVStorageProxy
        import vobject
        import uuid
        
        # Use storage proxy (will fallback to local if not configured)
        proxy = DAVStorageProxy(db, current_user.username, 'cardav')
        
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
                        
                        # Check if contact already exists using proxy
                        if proxy.file_exists(f"{contact_uid}.vcf"):
                            logger.debug(f"Contact {contact_uid} already exists, skipping")
                            skipped_count += 1
                            continue
                        
                        # Save to user's CardDAV directory using proxy
                        vcard_content = vcard_data if isinstance(vcard_data, str) else vcard_data.decode('utf-8')
                        if not proxy.write_file(f"{contact_uid}.vcf", vcard_content):
                            logger.warning(f"Failed to save contact {contact_uid}")
                            continue
                        
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
