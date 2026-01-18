"""
CalDAV/CardDAV Router - Import/Export endpoints for calendar and contacts.
"""
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
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
    """Export all calendar events as a single iCalendar (.ics) file. Uses storage proxy if configured."""
    try:
        from app.services.dav_storage_proxy import DAVStorageProxy
        from icalendar import Calendar
        
        # Use storage proxy (will fallback to local if not configured)
        proxy = DAVStorageProxy(db, current_user.username, 'caldav')
        
        # Create a new calendar
        cal = Calendar()
        cal.add('prodid', '-//PosterChan AI//CalDAV Export//EN')
        cal.add('version', '2.0')
        cal.add('calscale', 'GREGORIAN')
        cal.add('method', 'PUBLISH')
        cal.add('x-wr-calname', f'{current_user.username} Calendar')
        
        event_count = 0
        
        # Get all calendar directories and files
        def collect_events(subpath: str = ""):
            """Recursively collect events from calendar directories."""
            nonlocal event_count
            items = proxy.list_files(subpath)
            
            for item in items:
                name = item.get('name', '')
                item_type = item.get('type', 'file')
                
                if item_type == 'directory':
                    # Recursively process subdirectories (calendar subdirectories)
                    new_subpath = f"{subpath}/{name}" if subpath else name
                    collect_events(new_subpath)
                elif name.endswith('.ics'):
                    # Read and process .ics file
                    try:
                        filepath = f"{subpath}/{name}" if subpath else name
                        ics_data = proxy.read_file(filepath)
                        
                        if ics_data:
                            # Parse the iCalendar file
                            file_cal = Calendar.from_ical(ics_data)
                            
                            # Extract events/todos from the file
                            for component in file_cal.walk():
                                if component.name in ('VEVENT', 'VTODO'):
                                    cal.add_component(component)
                                    event_count += 1
                    except Exception as e:
                        logger.warning(f"Error reading {filepath}: {e}")
                        continue
        
        # Start collecting from root
        collect_events()
        
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
    calendar_name: str = Form(None),  # Optional - will auto-detect if not provided
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Import calendar events from iCalendar (.ics) file into a named calendar."""
    try:
        from icalendar import Calendar
        import uuid
        import pytz
        import re
        
        # Read uploaded file
        ics_data = await file.read()
        ics_data = ics_data.decode('utf-8')
        
        # Parse iCalendar data
        try:
            cal = Calendar.from_ical(ics_data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid iCalendar data: {str(e)}")
        
        # Auto-detect calendar name if not provided
        if not calendar_name:
            # Try to get calendar name from the .ics file
            detected_name = None
            
            # Check for X-WR-CALNAME or X-WR-CALENDAR-NAME
            if 'X-WR-CALNAME' in cal:
                detected_name = str(cal['X-WR-CALNAME'])
            elif 'X-WR-CALENDAR-NAME' in cal:
                detected_name = str(cal['X-WR-CALENDAR-NAME'])
            elif 'NAME' in cal:
                detected_name = str(cal['NAME'])
            
            # If still no name, use the filename (without .ics extension)
            if not detected_name:
                detected_name = file.filename.replace('.ics', '') if file.filename else "imported"
            
            calendar_name = detected_name
            logger.info(f"Auto-detected calendar name: {calendar_name}")
        
        # Sanitize calendar name
        calendar_name = re.sub(r'[^\w\s-]', '', calendar_name)
        calendar_name = calendar_name.replace(' ', '_')
        calendar_name = re.sub(r'_+', '_', calendar_name)
        calendar_name = calendar_name.strip('_').lower() or "default"
        
        # Use storage proxy (will fallback to local if not configured)
        from app.services.dav_storage_proxy import DAVStorageProxy
        proxy = DAVStorageProxy(db, current_user.username, 'caldav')
        
        # Calendar subdirectory path
        calendar_subpath = calendar_name
        
        imported_count = 0
        error_count = 0
        skipped_count = 0
        
        # Extract all VTIMEZONE components from the imported file
        vtimezones = {}
        for component in cal.walk():
            if component.name == "VTIMEZONE":
                tzid = str(component.get('TZID', ''))
                if tzid:
                    vtimezones[tzid] = component
        
        # Helper function to get or create VTIMEZONE
        def get_vtimezone(tzid):
            """Get VTIMEZONE component for a timezone ID."""
            if tzid in vtimezones:
                return vtimezones[tzid]
            
            # Try to create from pytz
            try:
                from datetime import datetime
                tz = pytz.timezone(tzid)
                now = datetime.now()
                
                # Create basic VTIMEZONE component
                from icalendar import Timezone
                vtimezone = Timezone()
                vtimezone.add('TZID', tzid)
                
                # Add to cache
                vtimezones[tzid] = vtimezone
                return vtimezone
            except:
                return None
        
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
                
                # Build filepath
                filepath = f"{calendar_subpath}/{event_uid}.ics" if calendar_subpath else f"{event_uid}.ics"
                
                # Check if event already exists
                if proxy.file_exists(filepath):
                    logger.debug(f"Event {event_uid} already exists, skipping")
                    skipped_count += 1
                    continue
                
                # Create a new calendar for this single event
                new_cal = Calendar()
                new_cal.add('prodid', '-//PosterChan AI//CalDAV Import//EN')
                new_cal.add('version', '2.0')
                
                # Find all timezone IDs referenced in this component
                component_tzids = set()
                for prop in component.property_items():
                    if hasattr(prop[1], 'params') and 'TZID' in prop[1].params:
                        component_tzids.add(prop[1].params['TZID'])
                
                # Add VTIMEZONE components for all referenced timezones
                for tzid in component_tzids:
                    vtimezone = get_vtimezone(tzid)
                    if vtimezone:
                        new_cal.add_component(vtimezone)
                
                # Add the event/todo component
                new_cal.add_component(component)
                
                # Save to file using proxy
                ical_content = new_cal.to_ical().decode('utf-8')
                success = proxy.write_file(filepath, ical_content)
                
                if not success:
                    logger.warning(f"Failed to save event {event_uid}")
                    error_count += 1
                    continue
                
                imported_count += 1
            except Exception as e:
                logger.warning(f"Error importing event: {e}")
                error_count += 1
                continue
        
        return {
            "success": True,
            "count": imported_count,
            "calendar": calendar_name,
            "message": f"Imported {imported_count} event(s) into '{calendar_name}', skipped {skipped_count}, errors {error_count}"
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


@carddav_router.post("/import")
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
