"""
CalDAV/CardDAV Router - Import/Export endpoints for calendar and contacts.
"""
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session
from pathlib import Path
from datetime import datetime
import logging

from app.database import get_db
from app.auth import get_current_user
from app.models import User

logger = logging.getLogger(__name__)

# CalDAV Router
caldav_router = APIRouter(prefix="/api/caldav", tags=["caldav"])


@caldav_router.get("/export")
async def export_calendar(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export all calendar events as a single iCalendar (.ics) file."""
    try:
        from app.services.caldav_server import get_user_caldav_path
        from icalendar import Calendar
        
        # Get user's CalDAV path
        caldav_path = get_user_caldav_path(current_user, db)
        
        if not caldav_path.exists():
            raise HTTPException(status_code=404, detail="CalDAV directory not found")
        
        # Create a new calendar
        cal = Calendar()
        cal.add('prodid', '-//PosterChan AI//CalDAV Export//EN')
        cal.add('version', '2.0')
        cal.add('calscale', 'GREGORIAN')
        cal.add('method', 'PUBLISH')
        cal.add('x-wr-calname', f'{current_user.username} Calendar')
        
        event_count = 0
        
        # Read all .ics files and add them to the calendar
        for ics_file in caldav_path.glob("*.ics"):
            try:
                with open(ics_file, 'r', encoding='utf-8') as f:
                    ics_data = f.read()
                
                # Parse the iCalendar file
                file_cal = Calendar.from_ical(ics_data)
                
                # Extract events/todos from the file
                for component in file_cal.walk():
                    if component.name in ('VEVENT', 'VTODO'):
                        cal.add_component(component)
                        event_count += 1
            except Exception as e:
                logger.warning(f"Error reading {ics_file}: {e}")
                continue
        
        if event_count == 0:
            raise HTTPException(status_code=404, detail="No events found to export")
        
        # Generate iCalendar content
        ics_content = cal.to_ical().decode('utf-8')
        
        # Return as downloadable file
        return Response(
            content=ics_content,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="calendar_{current_user.username}_{datetime.utcnow().strftime("%Y%m%d")}.ics"'
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting calendar: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to export calendar: {str(e)}")


@caldav_router.post("/import")
async def import_calendar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Import calendar events from iCalendar (.ics) file."""
    try:
        from app.services.caldav_server import get_user_caldav_path
        from icalendar import Calendar
        import uuid
        
        # Read uploaded file
        ics_data = await file.read()
        ics_data = ics_data.decode('utf-8')
        
        # Get user's CalDAV path
        caldav_path = get_user_caldav_path(current_user, db)
        caldav_path.mkdir(parents=True, exist_ok=True)
        
        imported_count = 0
        error_count = 0
        skipped_count = 0
        
        # Parse iCalendar data
        try:
            cal = Calendar.from_ical(ics_data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid iCalendar data: {str(e)}")
        
        # Import each event/todo
        for component in cal.walk():
            if component.name not in ('VEVENT', 'VTODO'):
                continue
            
            try:
                # Get or generate UID
                if 'UID' in component:
                    event_uid = str(component['UID'])
                else:
                    event_uid = str(uuid.uuid4())
                    component.add('UID', event_uid)
                
                # Check if event already exists
                ics_file = caldav_path / f"{event_uid}.ics"
                if ics_file.exists():
                    logger.debug(f"Event {event_uid} already exists, skipping")
                    skipped_count += 1
                    continue
                
                # Create a new calendar for this single event
                new_cal = Calendar()
                new_cal.add('prodid', '-//PosterChan AI//CalDAV Import//EN')
                new_cal.add('version', '2.0')
                new_cal.add_component(component)
                
                # Save to file
                with open(ics_file, 'wb') as f:
                    f.write(new_cal.to_ical())
                
                imported_count += 1
            except Exception as e:
                logger.warning(f"Error importing event: {e}")
                error_count += 1
                continue
        
        return {
            "success": True,
            "count": imported_count,
            "message": f"Imported {imported_count} event(s), skipped {skipped_count}, errors {error_count}"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error importing calendar: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to import calendar: {str(e)}")


# CardDAV Router
carddav_router = APIRouter(prefix="/api/carddav", tags=["carddav"])


@carddav_router.get("/export")
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


@carddav_router.post("/import")
async def import_contacts(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Import contacts from vCard (.vcf) file."""
    try:
        from app.services.cardav_server import get_user_cardav_path
        import vobject
        import uuid
        
        # Read uploaded file
        vcf_data = await file.read()
        vcf_data = vcf_data.decode('utf-8')
        
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
            "count": imported_count,
            "message": f"Imported {imported_count} contact(s), skipped {skipped_count}, errors {error_count}"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error importing contacts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to import contacts: {str(e)}")
