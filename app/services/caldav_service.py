"""
CalDAV/CardDAV Service - Calendar and Contacts integration.

Provides:
- CalDAV calendar access (list events, add events)
- CardDAV contact access (search contacts)
- Daily schedule summaries
"""
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass

import caldav
import vobject
import requests
from icalendar import Calendar, Event
from sqlalchemy.orm import Session
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import urllib3

from app.database import SessionLocal
from app.models import User, UserSetting

# Suppress SSL warnings for external CardDAV servers with certificate issues
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# Timeout for CalDAV operations (in seconds)
CALDAV_TIMEOUT = 15  # Reduced from 30 to prevent long hangs
CALDAV_OPERATION_TIMEOUT = 10  # Timeout for individual operations

# Thread pool for running blocking CalDAV operations with timeout
_caldav_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="caldav_worker")


# Define dataclasses before functions that use them
@dataclass
class CalendarEvent:
    """Represents a calendar event."""
    uid: str
    summary: str
    description: Optional[str]
    start: datetime
    end: Optional[datetime]
    location: Optional[str]
    calendar_name: str
    rrule: Optional[str] = None


@dataclass
class Contact:
    """Represents a contact."""
    uid: str
    name: str
    emails: List[str]  # Multiple email addresses
    phone: Optional[str]
    organization: Optional[str]
    address: Optional[str]  # Street address
    note: Optional[str]

    @property
    def email(self) -> Optional[str]:
        """Backwards compatibility - return first email."""
        return self.emails[0] if self.emails else None


def _get_events_from_builtin(user_id: int, start_date: datetime, end_date: datetime, calendar_name: str, db: Session) -> List[CalendarEvent]:
    """Get events directly from built-in CalDAV storage using storage proxy.
    
    When calendar_name is a specific calendar directory (e.g., 'main', 'wwwcanoncityschoolsorg'),
    only searches that directory. When calendar_name is 'Built-in Calendar' or 'Calendar',
    searches all directories.
    """
    try:
        from app.models import User
        from app.services.dav_storage_proxy import DAVStorageProxy
        from icalendar import Calendar as ICalendar
        from dateutil import parser as date_parser
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.error(f"[CalDAV] User {user_id} not found in database")
            return []
        
        # Use storage proxy (must be configured)
        proxy = DAVStorageProxy(db, user.username, 'caldav')
        
        # Check if proxy is configured
        if not proxy.use_proxy:
            logger.error(f"[CalDAV] Storage proxy NOT configured! Set storage_server_url in Admin settings.")
            logger.error(f"[CalDAV] Cannot fetch calendar events without storage server configured.")
            logger.error(f"[CalDAV] This is why cal week/month shows 0 events - storage server must be configured!")
            return []
        
        logger.info(f"[CalDAV] Storage proxy configured: {proxy.storage_url}")
        logger.info(f"[CalDAV] Searching for events from {start_date.date()} to {end_date.date()}")
        
        all_events = []
        
        # If calendar_name is a specific calendar directory (not None, "Built-in Calendar", or "Calendar"),
        # only search that specific directory
        # When calendar_name is None, we want to search ALL calendars
        if calendar_name and calendar_name not in ("Built-in Calendar", "Calendar", None):
            # This is a specific calendar subdirectory
            logger.info(f"[CalDAV] Getting events from specific calendar directory: '{calendar_name}'")
            cal_events = _get_events_from_calendar_dir(proxy, calendar_name, start_date, end_date, calendar_name)
            logger.info(f"[CalDAV] Found {len(cal_events)} events in directory '{calendar_name}'")
            all_events.extend(cal_events)
        else:
            # Search all calendar subdirectories and root
            # Built-in server may have multiple calendars (main, wwwcanoncityschoolsorg, etc.)
            # This happens when calendar_name is None, "Built-in Calendar", or "Calendar"
            logger.info(f"[CalDAV] Getting events from all calendars (calendar_name={calendar_name})")
            try:
                root_items = proxy.list_files("")
                logger.info(f"[CalDAV] Root items from storage proxy: {len(root_items)} items")
            except Exception as e:
                error_msg = str(e)
                if "Connection refused" in error_msg or "111" in error_msg:
                    logger.error(f"[CalDAV] Cannot connect to storage server: {e}")
                    logger.error(f"[CalDAV] Storage server may be down or unreachable. Check if it's running at {proxy.storage_url if hasattr(proxy, 'storage_url') else 'unknown'}")
                else:
                    logger.error(f"[CalDAV] Error listing root files: {e}", exc_info=True)
                return []
            
            # Debug: log all items found
            for item in root_items:
                logger.debug(f"[CalDAV] Root item: {item.get('name', 'UNKNOWN')} (is_dir={item.get('is_directory', False)})")
            
            calendar_dirs = []
            for item in root_items:
                if item.get('is_directory', False) and not item.get('name', '').startswith('.'):
                    calendar_dirs.append(item.get('name'))
            
            if not calendar_dirs and not root_items:
                logger.warning(f"[CalDAV] No files/folders found in caldav storage for user {user.username}!")
                logger.warning(f"[CalDAV] Check if caldav data exists on storage server at path: caldav/")
            
            # Search all calendar subdirectories
            logger.info(f"[CalDAV] Searching {len(calendar_dirs)} calendar directories: {calendar_dirs}")
            if calendar_dirs:
                for cal_dir in calendar_dirs:
                    logger.info(f"[CalDAV] Processing calendar directory: {cal_dir}")
                    try:
                        cal_events = _get_events_from_calendar_dir(proxy, cal_dir, start_date, end_date, cal_dir)
                        logger.info(f"[CalDAV] Found {len(cal_events)} events in calendar directory '{cal_dir}'")
                        if cal_events:
                            all_events.extend(cal_events)
                            # Log sample events for debugging
                            for i, event in enumerate(cal_events[:3]):
                                logger.info(f"[CalDAV]   Sample event {i+1} from '{cal_dir}': {event.summary} on {event.start.date()}")
                    except Exception as e:
                        logger.error(f"[CalDAV] Error processing calendar directory '{cal_dir}': {e}", exc_info=True)
            else:
                logger.warning(f"[CalDAV] No calendar directories found - checking root for legacy .ics files")
            
            # Also check root for legacy .ics files
            try:
                root_events = _get_events_from_calendar_dir(proxy, "", start_date, end_date, "Calendar")
                all_events.extend(root_events)
                logger.info(f"[CalDAV] Found {len(root_events)} events in root (legacy)")
            except Exception as e:
                logger.error(f"[CalDAV] Error checking root for legacy events: {e}", exc_info=True)
        
        logger.info(f"[CalDAV] Total events found in _get_events_from_builtin: {len(all_events)}")
        if all_events:
            logger.info(f"[CalDAV] Sample events being returned:")
            for i, event in enumerate(all_events[:5]):
                logger.info(f"[CalDAV]   Event {i+1}: {event.summary} on {event.start.date()} from '{event.calendar_name}'")
        return all_events
    except Exception as e:
        logger.error(f"Error getting events from built-in storage: {e}", exc_info=True)
        return []


def _get_events_from_calendar_dir(proxy, cal_dir: str, start_date: datetime, end_date: datetime, calendar_name: str) -> "List[CalendarEvent]":
    """Helper function to get events from a specific calendar directory.
    
    Handles recurring events by expanding RRULE to generate occurrences within the date range.
    """
    from icalendar import Calendar as ICalendar
    from dateutil.rrule import rrulestr
    
    logger.info(f"[CalDAV] _get_events_from_calendar_dir: dir='{cal_dir}', date_range={start_date.date()} to {end_date.date()}")
    
    try:
        file_items = proxy.list_files(cal_dir)
        logger.info(f"[CalDAV] Found {len(file_items)} items in directory '{cal_dir}'")
        if not file_items:
            logger.warning(f"[CalDAV] No items found in directory '{cal_dir}' - directory may be empty or not exist")
            return []
    except Exception as e:
        error_msg = str(e)
        # Check if it's a connection error
        if "Connection refused" in error_msg or "111" in error_msg:
            logger.error(f"[CalDAV] Cannot connect to storage server to list files in '{cal_dir}': {e}")
            logger.error(f"[CalDAV] Check if storage server is running and accessible at {proxy.storage_url if hasattr(proxy, 'storage_url') else 'unknown'}")
        else:
            logger.error(f"[CalDAV] Error listing files in '{cal_dir}': {e}", exc_info=True)
        return []
    
    events = []
    ics_count = 0
    
    # Ensure dates are timezone-aware for comparison
    # Convert to UTC for consistent comparison
    if start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)
    else:
        # Convert to UTC if timezone-aware
        start_date = start_date.astimezone(timezone.utc)
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    else:
        # Convert to UTC if timezone-aware
        end_date = end_date.astimezone(timezone.utc)
    
    logger.info(f"[CalDAV] Date range (UTC): {start_date} to {end_date}")
    logger.info(f"[CalDAV] Date range (local dates): {start_date.date()} to {end_date.date()}")
    
    for item in file_items:
        name = item.get('name', '')
        if not name.endswith('.ics'):
            continue
        ics_count += 1
        # Only log every 50th file to reduce logging overhead with large calendars
        if ics_count % 50 == 0 or ics_count <= 10:
            logger.info(f"[CalDAV] Processing .ics file #{ics_count}: {name}")
        else:
            logger.debug(f"[CalDAV] Processing .ics file #{ics_count}: {name}")
        
        try:
            # Read .ics file using proxy
            filepath = f"{cal_dir}/{name}" if cal_dir else name
            ical_data = proxy.read_file(filepath)
            if not ical_data:
                continue
            
            # Parse iCalendar data
            cal = ICalendar.from_ical(ical_data.encode('utf-8'))
            
            # Process all VEVENT components
            vevent_count = 0
            for component in cal.walk():
                if component.name == "VEVENT":
                    vevent_count += 1
                    summary = component.get('summary', 'No Title')
                    if vevent_count <= 5:  # Log first 5 events per file for debugging
                        logger.info(f"[CalDAV] Processing VEVENT #{vevent_count} from {name}: {summary}")
                    else:
                        logger.debug(f"[CalDAV] Processing VEVENT #{vevent_count} from {name}: {summary}")
                    # Get start time
                    dtstart = component.get('dtstart')
                    if not dtstart:
                        continue
                    
                    event_start = dtstart.dt
                    is_all_day = False
                    
                    # Handle both datetime and date objects (all-day events)
                    if isinstance(event_start, date) and not isinstance(event_start, datetime):
                        # All-day event - convert date to datetime at midnight
                        is_all_day = True
                        event_start = datetime.combine(event_start, datetime.min.time(), tzinfo=timezone.utc)
                    
                    # After conversion, event_start should be a datetime
                    if not isinstance(event_start, datetime):
                        # If it's still not a datetime after conversion, log and skip
                        logger.warning(f"[CalDAV] Event '{summary}' has invalid start time type: {type(event_start)}, value: {event_start}")
                        continue
                    
                    # Make timezone-aware if naive
                    if event_start.tzinfo is None:
                        event_start = event_start.replace(tzinfo=timezone.utc)
                    
                    # Get end time first (needed for date range check)
                    dtend = component.get('dtend')
                    event_end = None
                    if dtend:
                        event_end = dtend.dt
                        if isinstance(event_end, date) and not isinstance(event_end, datetime):
                            # All-day event end - convert to datetime at end of day
                            event_end = datetime.combine(event_end, datetime.max.time().replace(microsecond=0), tzinfo=timezone.utc)
                        elif isinstance(event_end, datetime):
                            if event_end.tzinfo is None:
                                event_end = event_end.replace(tzinfo=timezone.utc)
                    
                    # Check for recurring events (RRULE) - these need special handling
                    rrule_prop = component.get('rrule')
                    
                    # For non-recurring events, check if event overlaps with date range
                    # An event overlaps if: (event_start <= end_date) AND (event_end is None OR event_end >= start_date)
                    # For recurring events, we'll expand them later so only skip if completely impossible
                    if not rrule_prop:
                        # Check if event overlaps with the date range
                        # Event overlaps if it starts before or during the range AND (has no end OR ends during or after the range)
                        
                        # First check: event must start before or during the range
                        if event_start > end_date:
                            # Event starts after the range - skip it
                            summary = component.get('summary', 'No Title')
                            logger.debug(f"[CalDAV] Skipping event '{summary}' - starts after range: start={event_start.date()}, range end={end_date.date()}")
                            continue
                        
                        # Second check: if event has an end date, it must end during or after the range
                        if event_end is not None:
                            if event_end < start_date:
                                # Event ended before the range - skip it
                                summary = component.get('summary', 'No Title')
                                logger.debug(f"[CalDAV] Skipping event '{summary}' - ended before range: end={event_end.date()}, range start={start_date.date()}")
                                continue
                            # Event overlaps - include it
                            summary = component.get('summary', 'No Title')
                            logger.info(f"[CalDAV] Including event '{summary}': start={event_start.date()}, end={event_end.date()}, range={start_date.date()} to {end_date.date()}")
                        else:
                            # Event has no end date - check if it's relevant to the range
                            # Include if:
                            # 1. Event started during or after the range start (definitely relevant)
                            # 2. Event started before range but within 30 days (ongoing event, likely still relevant)
                            # This prevents very old events (like years ago) from appearing
                            days_before_range = (start_date - event_start).total_seconds() / 86400
                            if event_start >= start_date:
                                # Event started during or after the range start - include it
                                summary = component.get('summary', 'No Title')
                                logger.info(f"[CalDAV] Including event '{summary}': start={event_start.date()}, no end date, started during range")
                            elif 0 <= days_before_range <= 30:
                                # Event started within 30 days before the range - include it (it's ongoing and recent)
                                summary = component.get('summary', 'No Title')
                                logger.info(f"[CalDAV] Including ongoing event '{summary}': start={event_start.date()}, {days_before_range:.1f} days before range start")
                            else:
                                # Event started too long ago - skip it
                                summary = component.get('summary', 'No Title')
                                logger.debug(f"[CalDAV] Skipping old event with no end date '{summary}': start={event_start.date()}, {days_before_range:.1f} days before range start")
                                continue
                    else:
                        # Recurring event: only skip if it DEFINITELY can't have occurrences
                        # Check for UNTIL or COUNT limits that would exclude this range
                        rrule_text = rrule_prop.to_ical().decode('utf-8') if hasattr(rrule_prop, 'to_ical') else str(rrule_prop)
                        
                        # If UNTIL is specified and it's before our start_date, skip
                        if 'UNTIL=' in rrule_text:
                            try:
                                until_str = rrule_text.split('UNTIL=')[1].split(';')[0].split('\n')[0]
                                # Parse UNTIL date (format: YYYYMMDD or YYYYMMDDTHHMMSSZ)
                                if 'T' in until_str:
                                    until_date = datetime.strptime(until_str.replace('Z', ''), '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc)
                                else:
                                    until_date = datetime.strptime(until_str, '%Y%m%d').replace(tzinfo=timezone.utc)
                                if until_date < start_date:
                                    continue  # Recurrence ended before our range
                            except Exception:
                                pass  # If we can't parse, don't skip
                    
                    # Get event details
                    summary = str(component.get('summary', '')) if component.get('summary') else "No Title"
                    description = str(component.get('description', '')) if component.get('description') else None
                    location = str(component.get('location', '')) if component.get('location') else None
                    uid = str(component.get('uid', '')) if component.get('uid') else ""
                    
                    # Handle recurring vs non-recurring events (rrule_prop already fetched above)
                    if rrule_prop:
                        # This is a recurring event - expand occurrences
                        try:
                            # Calculate event duration for recurring instances
                            event_duration = None
                            if event_end:
                                event_duration = event_end - event_start
                            
                            # Build RRULE string from the property
                            rrule_str = rrule_prop.to_ical().decode('utf-8')
                            
                            # Create rrule with dtstart
                            rule = rrulestr(rrule_str, dtstart=event_start)
                            
                            # Get occurrences within the date range
                            # Limit to 500 occurrences to prevent excessive processing time
                            # This should be enough for monthly/yearly recurring events over reasonable periods
                            occurrences = list(rule.between(start_date, end_date, inc=True))[:500]
                            
                            for occurrence_start in occurrences:
                                occurrence_end = occurrence_start + event_duration if event_duration else None
                                
                                # Convert to naive local for CalendarEvent
                                occ_start_naive = to_naive_local(occurrence_start)
                                occ_end_naive = to_naive_local(occurrence_end) if occurrence_end else None
                                
                                event = CalendarEvent(
                                    uid=f"{uid}_{occurrence_start.isoformat()}",
                                    summary=summary,
                                    description=description,
                                    start=occ_start_naive,
                                    end=occ_end_naive,
                                    location=location,
                                    calendar_name=calendar_name
                                )
                                events.append(event)
                                logger.debug(f"[CalDAV] Added recurring occurrence: '{summary}' on {occ_start_naive.date()}")
                            
                            logger.debug(f"[CalDAV] Expanded recurring event '{summary}' to {len(occurrences)} occurrences")
                        except Exception as rrule_error:
                            logger.warning(f"[CalDAV] Failed to expand RRULE for '{summary}': {rrule_error}")
                            # Fall back to adding just the original event
                            event_start_naive = to_naive_local(event_start)
                            event_end_naive = to_naive_local(event_end) if event_end else None
                            events.append(CalendarEvent(
                                uid=uid,
                                summary=summary,
                                description=description,
                                start=event_start_naive,
                                end=event_end_naive,
                                location=location,
                                calendar_name=calendar_name
                            ))
                    else:
                        # Non-recurring event - add if it overlaps with range (already checked above)
                        # Convert to naive local for CalendarEvent
                        event_start_naive = to_naive_local(event_start)
                        event_end_naive = to_naive_local(event_end) if event_end else None
                        
                        event = CalendarEvent(
                            uid=uid,
                            summary=summary,
                            description=description,
                            start=event_start_naive,
                            end=event_end_naive,
                            location=location,
                            calendar_name=calendar_name
                        )
                        events.append(event)
                        logger.info(f"[CalDAV] Added non-recurring event: '{summary}' on {event_start_naive.date()}, total events now: {len(events)}")
        except Exception as e:
            logger.warning(f"[CalDAV] Error parsing event file {name}: {e}")
            continue
    
    logger.info(f"[CalDAV] Directory '{cal_dir}': {ics_count} .ics files processed, {len(events)} events found in date range")
    return events


def _save_event_to_builtin(user_id: int, db: Session, ical_data: str) -> bool:
    """Save event directly to built-in CalDAV storage (uses storage proxy if configured)."""
    try:
        from app.models import User
        from app.services.dav_storage_proxy import DAVStorageProxy
        from icalendar import Calendar as ICalendar
        import uuid
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        
        # Use storage proxy (will fallback to local if not configured)
        proxy = DAVStorageProxy(db, user.username, 'caldav')
        
        # Parse iCalendar to extract UID
        cal = ICalendar.from_ical(ical_data.encode('utf-8'))
        event_uid = None
        for component in cal.walk():
            if component.name in ("VEVENT", "VTODO"):
                event_uid = str(component.get('uid', uuid.uuid4()))
                break
        
        if not event_uid:
            event_uid = str(uuid.uuid4())
        
        # Save to file using proxy
        filepath = f"{event_uid}.ics"
        success = proxy.write_file(filepath, ical_data)
        
        if success:
            logger.info(f"Saved event to built-in storage: {filepath}")
        return success
    except Exception as e:
        logger.error(f"Error saving event to built-in storage: {e}")
        return False


def _save_contact_to_builtin(user_id: int, db: Session, vcard_data: str) -> bool:
    """Save contact directly to built-in CardDAV storage (uses storage proxy if configured)."""
    try:
        from app.models import User
        from app.services.dav_storage_proxy import DAVStorageProxy
        import vobject
        import uuid
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        
        # Use storage proxy (will fallback to local if not configured)
        proxy = DAVStorageProxy(db, user.username, 'cardav')
        
        # Parse vCard to extract UID
        try:
            vcard = vobject.readOne(vcard_data)
            contact_uid = str(vcard.uid.value) if hasattr(vcard, 'uid') else str(uuid.uuid4())
        except:
            contact_uid = str(uuid.uuid4())
        
        # Save to file using proxy
        filepath = f"{contact_uid}.vcf"
        success = proxy.write_file(filepath, vcard_data)
        
        if success:
            logger.info(f"Saved contact to built-in storage: {filepath}")
        return success
    except Exception as e:
        logger.error(f"Error saving contact to built-in storage: {e}")
        return False


def _edit_contact_builtin(user_id: int, db: Session, contact_uid: str, updates: dict) -> bool:
    """Edit contact directly in built-in CardDAV storage (uses storage proxy if configured)."""
    try:
        from app.models import User
        from app.services.dav_storage_proxy import DAVStorageProxy
        import vobject
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        
        # Use storage proxy (will fallback to local if not configured)
        proxy = DAVStorageProxy(db, user.username, 'cardav')
        
        filepath = f"{contact_uid}.vcf"
        
        # Check if file exists
        if not proxy.file_exists(filepath):
            logger.warning(f"Contact file not found: {filepath}")
            return False
        
        # Read and parse existing contact
        vcard_data = proxy.read_file(filepath)
        if not vcard_data:
            logger.warning(f"Failed to read contact file: {filepath}")
            return False
        
        vcard = vobject.readOne(vcard_data)
        
        # Update fields
        if 'name' in updates:
            vcard.fn.value = updates['name']
            name_parts = updates['name'].split()
            if hasattr(vcard, 'n'):
                vcard.n.value = vobject.vcard.Name(
                    family=name_parts[-1] if name_parts else '',
                    given=name_parts[0] if len(name_parts) > 1 else ''
                )
        
        if 'phone' in updates:
            if hasattr(vcard, 'tel'):
                vcard.tel.value = updates['phone']
            else:
                vcard.add('tel')
                vcard.tel.value = updates['phone']
                vcard.tel.type_param = 'CELL'
        
        if 'email' in updates:
            if hasattr(vcard, 'email'):
                vcard.email.value = updates['email']
            else:
                vcard.add('email')
                vcard.email.value = updates['email']
                vcard.email.type_param = 'INTERNET'
        
        if 'organization' in updates:
            if hasattr(vcard, 'org'):
                vcard.org.value = [updates['organization']]
            else:
                vcard.add('org')
                vcard.org.value = [updates['organization']]
        
        if 'address' in updates:
            # Parse address string into components
            address_str = updates.get('address', '').strip()
            if address_str:
                # Try to parse address components (simple: assume "street, city, state zip, country")
                address_parts = address_str.split(',')
                if len(address_parts) >= 2:
                    street = address_parts[0].strip()
                    city = address_parts[1].strip() if len(address_parts) > 1 else ''
                    state = address_parts[2].strip() if len(address_parts) > 2 else ''
                    postal = address_parts[3].strip() if len(address_parts) > 3 else ''
                    country = address_parts[4].strip() if len(address_parts) > 4 else ''
                    # If state contains zip, split it
                    if state and ' ' in state:
                        state_parts = state.rsplit(' ', 1)
                        state = state_parts[0]
                        postal = state_parts[1] if len(state_parts) > 1 else postal
                else:
                    # Single line address - put it in street field
                    street = address_str
                    city = state = postal = country = ''
                
                # Create proper vobject Address object
                new_adr = vobject.vcard.Address(
                    street=street,
                    city=city,
                    region=state,
                    code=postal,
                    country=country,
                    box='',
                    extended=''
                )
                
                # Remove existing ADR fields first
                if hasattr(vcard, 'adr'):
                    if isinstance(vcard.adr, list):
                        for adr in list(vcard.adr):
                            vcard.remove(adr)
                    else:
                        vcard.remove(vcard.adr)
                
                # Add new address - after removing all, add() should create a single property
                vcard.add('adr')
                # Access the newly added property (should be single, not list, since we removed all)
                if hasattr(vcard, 'adr'):
                    if isinstance(vcard.adr, list):
                        # If still a list (shouldn't happen after removal), use first element
                        adr_prop = vcard.adr[0] if vcard.adr else None
                    else:
                        adr_prop = vcard.adr
                    
                    if adr_prop:
                        adr_prop.value = new_adr
                        adr_prop.type_param = 'HOME'
            else:
                # Remove address if empty
                if hasattr(vcard, 'adr'):
                    if isinstance(vcard.adr, list):
                        for adr in list(vcard.adr):
                            vcard.remove(adr)
                    else:
                        vcard.remove(vcard.adr)
        
        if 'note' in updates:
            if hasattr(vcard, 'note'):
                vcard.note.value = updates['note']
            else:
                vcard.add('note')
                vcard.note.value = updates['note']
        
        # Save updated contact using proxy
        success = proxy.write_file(filepath, vcard.serialize())
        
        if success:
            logger.info(f"Updated contact {contact_uid} in built-in storage")
        return success
    except Exception as e:
        logger.error(f"Error editing contact in built-in storage: {e}")
        return False


def _delete_contact_builtin(user_id: int, db: Session, contact_uid: str) -> bool:
    """Delete contact directly from built-in CardDAV storage (uses storage proxy if configured)."""
    try:
        from app.models import User
        from app.services.dav_storage_proxy import DAVStorageProxy
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        
        # Use storage proxy (will fallback to local if not configured)
        proxy = DAVStorageProxy(db, user.username, 'cardav')
        
        filepath = f"{contact_uid}.vcf"
        success = proxy.delete_file(filepath)
        
        if success:
            logger.info(f"Deleted contact {contact_uid} from built-in storage")
        else:
            logger.warning(f"Contact file not found or failed to delete: {filepath}")
        
        return success
    except Exception as e:
        logger.error(f"Error deleting contact from built-in storage: {e}")
        return False


def run_with_timeout(func, timeout=CALDAV_OPERATION_TIMEOUT, *args, **kwargs):
    """Run a blocking function with timeout using thread pool."""
    try:
        future = _caldav_executor.submit(func, *args, **kwargs)
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        logger.warning(f"CalDAV operation {func.__name__} timed out after {timeout}s")
        return None
    except Exception as e:
        logger.error(f"CalDAV operation {func.__name__} failed: {e}")
        return None


class TimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    """HTTP Adapter with default timeout."""
    def __init__(self, timeout=CALDAV_TIMEOUT, *args, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs['timeout'] = kwargs.get('timeout', self.timeout)
        return super().send(request, **kwargs)


def create_caldav_client(url: str, username: str, password: str) -> caldav.DAVClient:
    """Create a CalDAV client with timeout configured."""
    # Create caldav client with timeout via requests session
    try:
        # Create a custom session with timeout
        session = requests.Session()
        adapter = TimeoutHTTPAdapter(timeout=CALDAV_TIMEOUT)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        session.auth = (username, password)

        # Try to create client with custom session (caldav >= 1.0)
        return caldav.DAVClient(url=url, username=username, password=password, session=session)
    except TypeError:
        # Older caldav version doesn't support session parameter
        return caldav.DAVClient(url=url, username=username, password=password)




def get_user_calendars(user_id: int, db: Session = None) -> List[Dict[str, str]]:
    """Get user's configured CalDAV calendars. Always prioritizes built-in server if enabled."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        # ALWAYS check built-in CalDAV first - this is the native server
        use_builtin = db.query(UserSetting).filter(
            UserSetting.user_id == user_id,
            UserSetting.key == "use_builtin_caldav"
        ).first()
        
        # If built-in is explicitly enabled OR not set (default to built-in), use built-in
        if not use_builtin or use_builtin.value == "true":
            # Return built-in CalDAV config (native server)
            from app.models import User
            from app.models import Setting
            
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                # Get the CalDAV server URL from settings
                caldav_port = db.query(Setting).filter(Setting.key == "caldav_port").first()
                port = caldav_port.value if caldav_port else "8081"
                
                # For built-in server, use localhost since commands run on same server
                base_url = f"http://localhost:{port}/caldav/{user.username}/"
                
                # Discover all calendar subdirectories to return one entry per calendar
                try:
                    from app.services.dav_storage_proxy import DAVStorageProxy
                    # Create proxy with a fresh session to avoid session issues
                    proxy = DAVStorageProxy(db, user.username, 'caldav')
                    root_items = proxy.list_files("")
                    calendar_dirs = []
                    for item in root_items:
                        if item.get('is_directory', False) and not item.get('name', '').startswith('.'):
                            calendar_dirs.append(item.get('name'))
                    
                    calendars = []
                    # Return one calendar entry per subdirectory
                    for cal_dir in calendar_dirs:
                        calendars.append({
                            "name": cal_dir,  # Use actual calendar directory name
                            "url": f"{base_url}{cal_dir}/",
                            "username": user.username,
                            "password": "__USE_SESSION_AUTH__",
                            "builtin": True
                        })
                    
                    # If no subdirectories, check for legacy .ics files in root
                    # Reuse root_items we already fetched instead of calling list_files again
                    if not calendar_dirs:
                        has_ics_files = any(item.get('name', '').endswith('.ics') for item in root_items)
                        if has_ics_files:
                            calendars.append({
                                "name": "Calendar",  # Legacy calendar name
                                "url": base_url,
                                "username": user.username,
                                "password": "__USE_SESSION_AUTH__",
                                "builtin": True
                            })
                    
                    # If still no calendars, return default
                    if not calendars:
                        calendars.append({
                            "name": "Built-in Calendar",
                            "url": base_url,
                            "username": user.username,
                            "password": "__USE_SESSION_AUTH__",
                            "builtin": True
                        })
                    
                    logger.info(f"[CalDAV] Using built-in (native) CalDAV server for user {user.username}, found {len(calendars)} calendars: {[c['name'] for c in calendars]}")
                    if len(calendars) == 0:
                        logger.warning(f"[CalDAV] No calendars discovered for user {user.username} - returning default calendar")
                    return calendars
                except Exception as e:
                    logger.error(f"[CalDAV] Error discovering calendars for built-in server: {e}", exc_info=True)
                    # Fallback to single calendar
                    return [{
                        "name": "Built-in Calendar",
                        "url": base_url,
                        "username": user.username,
                        "password": "__USE_SESSION_AUTH__",
                        "builtin": True
                    }]
        
        # Only use external CalDAV if built-in is explicitly disabled
        logger.debug(f"[CalDAV] Built-in disabled, checking external CalDAV calendars for user {user_id}")
        calendars = []
        # Get calendar settings (stored as caldav_calendars JSON)
        setting = db.query(UserSetting).filter(
            UserSetting.user_id == user_id,
            UserSetting.key == "caldav_calendars"
        ).first()

        if setting and setting.value:
            import json
            try:
                calendars = json.loads(setting.value)
                # Mark as external
                for cal in calendars:
                    cal["builtin"] = False
                logger.debug(f"[CalDAV] Found {len(calendars)} external calendars for user {user_id}")
            except json.JSONDecodeError:
                logger.error(f"Invalid caldav_calendars JSON for user {user_id}")

        # Default to built-in if no external calendars
        if not calendars:
            logger.debug(f"[CalDAV] No external calendars, defaulting to built-in for user {user_id}")
            from app.models import User
            from app.models import Setting
            from app.services.dav_storage_proxy import DAVStorageProxy
            
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                caldav_port = db.query(Setting).filter(Setting.key == "caldav_port").first()
                port = caldav_port.value if caldav_port else "8081"
                base_url = f"http://localhost:{port}/caldav/{user.username}/"
                
                # Discover all calendar subdirectories
                try:
                    proxy = DAVStorageProxy(db, user.username, 'caldav')
                    root_items = proxy.list_files("")
                    calendar_dirs = []
                    for item in root_items:
                        if item.get('is_directory', False) and not item.get('name', '').startswith('.'):
                            calendar_dirs.append(item.get('name'))
                    
                    builtin_calendars = []
                    # Return one calendar entry per subdirectory
                    for cal_dir in calendar_dirs:
                        builtin_calendars.append({
                            "name": cal_dir,
                            "url": f"{base_url}{cal_dir}/",
                            "username": user.username,
                            "password": "__USE_SESSION_AUTH__",
                            "builtin": True
                        })
                    
                    # If no subdirectories, check for legacy .ics files in root
                    if not calendar_dirs:
                        root_items = proxy.list_files("")
                        has_ics_files = any(item.get('name', '').endswith('.ics') for item in root_items)
                        if has_ics_files:
                            builtin_calendars.append({
                                "name": "Calendar",
                                "url": base_url,
                                "username": user.username,
                                "password": "__USE_SESSION_AUTH__",
                                "builtin": True
                            })
                    
                    # If still no calendars, return default
                    if not builtin_calendars:
                        builtin_calendars.append({
                            "name": "Built-in Calendar",
                            "url": base_url,
                            "username": user.username,
                            "password": "__USE_SESSION_AUTH__",
                            "builtin": True
                        })
                    
                    logger.debug(f"[CalDAV] Defaulting to built-in, found {len(builtin_calendars)} calendars: {[c['name'] for c in builtin_calendars]}")
                    return builtin_calendars
                except Exception as e:
                    logger.error(f"[CalDAV] Error discovering calendars for built-in server: {e}", exc_info=True)
                    # Fallback to single calendar
                    return [{
                        "name": "Built-in Calendar",
                        "url": base_url,
                        "username": user.username,
                        "password": "__USE_SESSION_AUTH__",
                        "builtin": True
                    }]

        return calendars
    finally:
        if close_db:
            db.close()


def get_user_contacts_config(user_id: int, db: Session = None) -> Optional[Dict[str, str]]:
    """Get user's CardDAV contacts configuration. Always prioritizes built-in server if enabled."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        # ALWAYS check built-in CardDAV first - this is the native server
        use_builtin = db.query(UserSetting).filter(
            UserSetting.user_id == user_id,
            UserSetting.key == "use_builtin_cardav"
        ).first()
        
        # If built-in is explicitly enabled OR not set (default to built-in), use built-in
        if not use_builtin or use_builtin.value == "true":
            # Return built-in CardDAV config (native server)
            from app.models import User
            from app.models import Setting
            
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                # Get the CardDAV server URL from settings
                cardav_port = db.query(Setting).filter(Setting.key == "cardav_port").first()
                port = cardav_port.value if cardav_port else "8082"
                
                # For built-in server, use localhost since commands run on same server
                url = f"http://localhost:{port}/carddav/{user.username}/"
                
                logger.debug(f"[CardDAV] Using built-in (native) CardDAV server for user {user.username}")
                return {
                    "url": url,
                    "username": user.username,
                    "password": "__USE_SESSION_AUTH__",  # Special marker for built-in auth
                    "name": "Built-in CardDAV",
                    "builtin": True
                }
        
        # Only use external CardDAV if built-in is explicitly disabled
        logger.debug(f"[CardDAV] Built-in disabled, checking external CardDAV config for user {user_id}")
        setting = db.query(UserSetting).filter(
            UserSetting.user_id == user_id,
            UserSetting.key == "carddav_config"
        ).first()

        if setting and setting.value:
            import json
            try:
                config = json.loads(setting.value)
                config["builtin"] = False
                logger.debug(f"[CardDAV] Using external CardDAV: {config.get('url', 'unknown')}")
                return config
            except json.JSONDecodeError:
                logger.error(f"Invalid carddav_config JSON for user {user_id}")

        # Default to built-in if no external config
        logger.debug(f"[CardDAV] No external config, defaulting to built-in for user {user_id}")
        from app.models import User
        from app.models import Setting
        
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            cardav_port = db.query(Setting).filter(Setting.key == "cardav_port").first()
            port = cardav_port.value if cardav_port else "8082"
            url = f"http://localhost:{port}/carddav/{user.username}/"
            
            return {
                "url": url,
                "username": user.username,
                "password": "__USE_SESSION_AUTH__",
                "name": "Built-in CardDAV",
                "builtin": True
            }

        return None
    finally:
        if close_db:
            db.close()


def connect_calendar(url: str, username: str, password: str) -> Optional[caldav.DAVClient]:
    """Connect to a CalDAV server."""
    try:
        client = create_caldav_client(url, username, password)
        # Test connection
        client.principal()
        return client
    except Exception as e:
        logger.error(f"Failed to connect to CalDAV server {url}: {e}")
        return None


def to_naive_local(dt) -> datetime:
    """Convert any datetime to naive local datetime."""
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        # It's a date, convert to datetime
        return datetime.combine(dt, datetime.min.time())
    if dt.tzinfo is not None:
        # Convert to local time and strip timezone
        local_dt = dt.astimezone().replace(tzinfo=None)
        return local_dt
    # Naive datetime - assume local time
    return dt


def to_local_aware(dt) -> datetime:
    """Convert datetime to local timezone-aware datetime."""
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        # It's a date, convert to datetime at midnight
        dt = datetime.combine(dt, datetime.min.time())
    if dt.tzinfo is None:
        # Naive datetime - treat as local time by explicitly assigning local timezone
        # Using replace() preserves the time value (6PM stays 6PM)
        # Unlike astimezone() which can cause conversion issues
        local_tz = datetime.now(timezone.utc).astimezone().tzinfo
        return dt.replace(tzinfo=local_tz)
    # Already timezone-aware - convert to local timezone
    return dt.astimezone()


def get_events_for_date_range(
    url: str,
    username: str,
    password: str,
    start_date: datetime,
    end_date: datetime,
    calendar_name: str = "Calendar"
) -> List[CalendarEvent]:
    """Get events from a CalDAV calendar for a date range."""
    events = []

    # Store timezone-aware versions for filtering
    start_date_aware = start_date if start_date.tzinfo else start_date.replace(tzinfo=timezone.utc)
    end_date_aware = end_date if end_date.tzinfo else end_date.replace(tzinfo=timezone.utc)
    
    # Convert to naive for caldav library (it expects naive datetimes)
    start_date_naive = to_naive_local(start_date)
    end_date_naive = to_naive_local(end_date)

    try:
        client = create_caldav_client(url, username, password)
        principal = client.principal()

        # Try to get calendars from the URL directly first
        try:
            calendar = caldav.Calendar(client=client, url=url)
            calendars = [calendar]
        except Exception:
            calendars = principal.calendars()

        for cal in calendars:
            try:
                # Use date_search with expand=True to get all events in range
                # Note: caldav library's date_search should handle overlapping events, but we'll also filter manually
                cal_events = cal.date_search(start=start_date_naive, end=end_date_naive, expand=True)
                for event in cal_events:
                    try:
                        vevent = event.vobject_instance.vevent

                        # Get start time
                        start = vevent.dtstart.value

                        # Handle naive datetimes - assume they're UTC if from CalDAV
                        if isinstance(start, datetime):
                            if start.tzinfo is None:
                                start_aware = start.replace(tzinfo=timezone.utc)
                            else:
                                start_aware = start
                        else:
                            # It's a date, convert to datetime
                            start_aware = datetime.combine(start, datetime.min.time()).replace(tzinfo=timezone.utc)
                        
                        # Get end time
                        end_dt = None
                        end_aware = None
                        if hasattr(vevent, 'dtend'):
                            end_val = vevent.dtend.value
                            if isinstance(end_val, datetime):
                                if end_val.tzinfo is None:
                                    end_aware = end_val.replace(tzinfo=timezone.utc)
                                else:
                                    end_aware = end_val
                                end_dt = to_naive_local(end_aware)
                            elif end_val:
                                # It's a date
                                end_aware = datetime.combine(end_val, datetime.min.time()).replace(tzinfo=timezone.utc)
                                end_dt = to_naive_local(end_aware)

                        # Apply same filtering logic as built-in server
                        # Check if event overlaps with range
                        if start_aware > end_date_aware:
                            # Event starts after the range - skip
                            continue
                        if end_aware is not None and end_aware < start_date_aware:
                            # Event ends before the range - skip
                            continue
                        if end_aware is None:
                            # Event has no end date - check if it's relevant to the range
                            # Include if started during/after range OR within 30 days before (ongoing and recent)
                            from datetime import timedelta
                            days_before_range = (start_date_aware - start_aware).total_seconds() / 86400
                            if start_aware >= start_date_aware:
                                # Event started during or after the range start - include it
                                pass  # Continue to add event
                            elif 0 <= days_before_range <= 30:
                                # Event started within 30 days before the range - include it (it's ongoing and recent)
                                pass  # Continue to add event
                            else:
                                # Event started too long ago - skip it
                                continue

                        events.append(CalendarEvent(
                            uid=str(vevent.uid.value) if hasattr(vevent, 'uid') else "",
                            summary=str(vevent.summary.value) if hasattr(vevent, 'summary') else "No Title",
                            description=str(vevent.description.value) if hasattr(vevent, 'description') else None,
                            start=to_naive_local(start_aware),
                            end=end_dt,
                            location=str(vevent.location.value) if hasattr(vevent, 'location') else None,
                            calendar_name=calendar_name
                        ))
                    except Exception as e:
                        logger.debug(f"Error parsing event: {e}")
                        continue
            except Exception as e:
                logger.debug(f"Error fetching events from calendar: {e}")
                continue

    except Exception as e:
        logger.error(f"Failed to get events from {url}: {e}")

    logger.info(f"[CalDAV] get_events_for_date_range: Found {len(events)} events from {url}")
    return events


def get_all_user_events(
    user_id: int,
    start_date: datetime,
    end_date: datetime,
    db: Session = None
) -> List[CalendarEvent]:
    """Get events from all user's calendars for a date range with timeout protection. Uses storage proxy for built-in server."""
    calendars = get_user_calendars(user_id, db)
    logger.info(f"[Calendar] Found {len(calendars)} calendars for user {user_id}: {[c.get('name', 'Unknown') for c in calendars]}")
    all_events = []
    
    # Track if we've already fetched from built-in calendars (to avoid duplicates)
    builtin_fetched = False

    for cal_config in calendars:
        url = cal_config.get('url', '')
        username = cal_config.get('username', '')
        password = cal_config.get('password', '')
        name = cal_config.get('name', 'Calendar')
        is_builtin = cal_config.get('builtin', False)

        logger.info(f"[Calendar] Processing calendar: name={name}, url={url}, is_builtin={is_builtin}")

        if url and username:
            try:
                # If using built-in server, read directly from storage proxy
                if is_builtin and password == "__USE_SESSION_AUTH__":
                    # Only fetch from built-in calendars once (they all share the same storage)
                    if not builtin_fetched:
                        logger.info(f"[Calendar] Fetching from built-in CalDAV server (all calendars)")
                        # Search all calendar directories to get all events
                        # Use timeout protection to prevent hangs with large calendars
                        def fetch_builtin():
                            result = _get_events_from_builtin(user_id, start_date, end_date, None, db)
                            logger.info(f"[Calendar] fetch_builtin returned {len(result) if result else 0} events")
                            return result
                        
                        events = run_with_timeout(fetch_builtin, timeout=CALDAV_OPERATION_TIMEOUT * 2)  # Give built-in more time (20s) since it processes many files
                        if events is None:
                            logger.warning(f"[Calendar] Built-in calendar fetch timed out or returned None after {CALDAV_OPERATION_TIMEOUT * 2}s")
                            events = []
                        elif not isinstance(events, list):
                            logger.error(f"[Calendar] Built-in calendar fetch returned non-list type: {type(events)}, value: {events}")
                            events = []
                        logger.info(f"[Calendar] Found {len(events) if events else 0} events from built-in server (all calendars) in range {start_date.date()} to {end_date.date()}")
                        if events and len(events) > 0:
                            all_events.extend(events)
                            logger.info(f"[Calendar] Added {len(events)} events from all built-in calendars, total now: {len(all_events)}")
                            # Log first few event summaries for debugging
                            for i, event in enumerate(events[:5]):
                                logger.info(f"[Calendar]   Event {i+1}: {event.summary} on {event.start.date()} from calendar '{event.calendar_name}'")
                        else:
                            logger.warning(f"[Calendar] No events found in any built-in calendar for date range {start_date.date()} to {end_date.date()}")
                            if events is not None and len(events) == 0:
                                logger.warning(f"[Calendar] Events list is empty - this might indicate a date filtering issue or timeout")
                        builtin_fetched = True
                    else:
                        logger.debug(f"[Calendar] Skipping duplicate built-in calendar config '{name}' (already fetched)")
                else:
                    # External CalDAV server - use HTTP requests with timeout
                    def fetch_calendar():
                        return get_events_for_date_range(url, username, password, start_date, end_date, name)

                    events = run_with_timeout(fetch_calendar, timeout=CALDAV_OPERATION_TIMEOUT)
                    if events:
                        all_events.extend(events)
                        logger.info(f"[Calendar] Added {len(events)} events from external calendar '{name}', total now: {len(all_events)}")
                    else:
                        logger.warning(f"[Calendar] Skipping calendar '{name}' - fetch timed out or failed")
            except Exception as e:
                logger.error(f"[Calendar] Error fetching from calendar '{name}': {e}", exc_info=True)
                # Continue to next calendar instead of failing completely

    # Sort by start time
    all_events.sort(key=lambda e: e.start)
    
    # Log summary of all events found
    logger.info(f"[Calendar] ===== FINAL SUMMARY =====")
    logger.info(f"[Calendar] Total calendars processed: {len(calendars)}")
    logger.info(f"[Calendar] Total events found across all calendars: {len(all_events)}")
    if all_events:
        # Group events by calendar for summary
        from collections import defaultdict
        events_by_calendar = defaultdict(list)
        for event in all_events:
            events_by_calendar[event.calendar_name].append(event)
        for cal_name, cal_events in events_by_calendar.items():
            logger.info(f"[Calendar]   - {cal_name}: {len(cal_events)} events")
        # Log all event summaries
        for event in all_events:
            logger.info(f"[Calendar]   Event: {event.summary} on {event.start.date()} from calendar '{event.calendar_name}'")
    else:
        logger.warning(f"[Calendar] WARNING: No events found in any calendar for date range {start_date.date()} to {end_date.date()}")
    logger.info(f"[Calendar] =========================")
    
    return all_events


def add_event_to_calendar(
    url: str,
    username: str,
    password: str,
    summary: str,
    description: str,
    start_time: datetime,
    end_time: Optional[datetime] = None,
    location: Optional[str] = None,
    rrule: Optional[str] = None,
    user_id: Optional[int] = None,
    db: Optional[Session] = None
) -> bool:
    """Add an event to a CalDAV calendar.

    Args:
        rrule: Optional iCalendar RRULE string (e.g., "FREQ=WEEKLY;BYDAY=MO,WE,FR")
        user_id: Optional user ID for built-in mode
        db: Optional database session for built-in mode
    """
    try:
        logger.info(f"Adding event '{summary}' to calendar at {url}")
        logger.debug(f"Event times: start={start_time}, end={end_time}, rrule={rrule}")
        
        # Check if using built-in server (direct file save)
        if password == "__USE_SESSION_AUTH__" and user_id and db:
            logger.info("Using built-in CalDAV storage (direct file save)")
            
            # Create event
            if end_time is None:
                end_time = start_time + timedelta(hours=1)

            # Convert to local timezone-aware, then to UTC for storage
            start_time = to_local_aware(start_time)
            end_time = to_local_aware(end_time)
            start_utc = start_time.astimezone(tz.utc)
            end_utc = end_time.astimezone(tz.utc)

            cal = Calendar()
            cal.add('prodid', '-//Posterchanai//Calendar//EN')
            cal.add('version', '2.0')

            event = Event()
            event.add('summary', summary)
            event.add('dtstart', start_utc)
            event.add('dtend', end_utc)
            if description:
                event.add('description', description)
            if location:
                event.add('location', location)
            if rrule:
                from icalendar import vRecur
                try:
                    rrule_dict = {}
                    for part in rrule.split(';'):
                        if '=' in part:
                            key, value = part.split('=', 1)
                            if key.upper() == 'BYDAY':
                                rrule_dict[key.lower()] = value.split(',')
                            elif value.isdigit():
                                rrule_dict[key.lower()] = int(value)
                            else:
                                rrule_dict[key.lower()] = value
                    event.add('rrule', rrule_dict)
                except Exception as e:
                    logger.warning(f"Failed to parse RRULE '{rrule}': {e}")
            event.add('dtstamp', datetime.now())

            import uuid
            event_uid = str(uuid.uuid4())
            event.add('uid', event_uid)

            cal.add_component(event)
            ical_data = cal.to_ical().decode('utf-8')
            
            return _save_event_to_builtin(user_id, db, ical_data)

        # Otherwise use CalDAV protocol (for external servers)
        client = create_caldav_client(url, username, password)

        # Try to get calendar from URL directly
        try:
            calendar = caldav.Calendar(client=client, url=url)
            logger.debug(f"Using calendar URL directly: {url}")
        except Exception as e:
            logger.debug(f"Direct URL failed ({e}), falling back to principal")
            principal = client.principal()
            calendars = principal.calendars()
            if not calendars:
                logger.error("No calendars found")
                return False
            calendar = calendars[0]
            logger.debug(f"Using first available calendar: {calendar.url}")

        # Create event
        if end_time is None:
            end_time = start_time + timedelta(hours=1)

        # Convert to local timezone-aware, then to UTC for storage
        # This ensures consistent handling across CalDAV servers
        start_time = to_local_aware(start_time)
        end_time = to_local_aware(end_time)

        # Convert to UTC for iCalendar storage
        start_utc = start_time.astimezone(tz.utc)
        end_utc = end_time.astimezone(tz.utc)

        logger.info(f"Storing event: local={start_time} -> UTC={start_utc}")

        cal = Calendar()
        cal.add('prodid', '-//Posterchanai//Calendar//EN')
        cal.add('version', '2.0')

        event = Event()
        event.add('summary', summary)
        event.add('dtstart', start_utc)
        event.add('dtend', end_utc)
        if description:
            event.add('description', description)
        if location:
            event.add('location', location)
        if rrule:
            # Parse RRULE string into components for icalendar
            from icalendar import vRecur
            try:
                # Parse the RRULE string (e.g., "FREQ=WEEKLY;BYDAY=MO,WE,FR")
                rrule_dict = {}
                for part in rrule.split(';'):
                    if '=' in part:
                        key, value = part.split('=', 1)
                        # Handle BYDAY which can have multiple values
                        if key.upper() == 'BYDAY':
                            rrule_dict[key.lower()] = value.split(',')
                        elif value.isdigit():
                            rrule_dict[key.lower()] = int(value)
                        else:
                            rrule_dict[key.lower()] = value
                event.add('rrule', rrule_dict)
                logger.info(f"Added RRULE: {rrule_dict}")
            except Exception as e:
                logger.warning(f"Failed to parse RRULE '{rrule}': {e}")
        event.add('dtstamp', datetime.now())

        import uuid
        event_uid = str(uuid.uuid4())
        event.add('uid', event_uid)

        cal.add_component(event)

        ical_data = cal.to_ical().decode('utf-8')
        logger.debug(f"iCal data:\n{ical_data[:500]}...")

        calendar.save_event(ical_data)
        logger.info(f"Successfully added event: {summary} (UID: {event_uid})")
        return True

    except Exception as e:
        logger.error(f"Failed to add event: {e}", exc_info=True)
        return False


def get_event_by_uid(
    url: str,
    username: str,
    password: str,
    event_uid: str
) -> Optional[CalendarEvent]:
    """Get a single event by UID."""
    try:
        client = create_caldav_client(url, username, password)

        try:
            calendar = caldav.Calendar(client=client, url=url)
            calendars = [calendar]
        except Exception:
            principal = client.principal()
            calendars = principal.calendars()

        for cal in calendars:
            try:
                # Try direct UID lookup
                try:
                    event = cal.event_by_uid(event_uid)
                    if event:
                        vevent = event.vobject_instance.vevent
                        start_dt = vevent.dtstart.value
                        if not isinstance(start_dt, datetime):
                            start_dt = datetime.combine(start_dt, datetime.min.time())
                        # Naive datetime from CalDAV is typically UTC
                        if isinstance(start_dt, datetime) and start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=timezone.utc)
                            logger.info(f"get_event_by_uid: assumed UTC for naive start: {start_dt}")
                        # Convert to local timezone
                        start_dt = to_local_aware(start_dt)
                        logger.info(f"get_event_by_uid: start converted to local: {start_dt}")

                        end_dt = None
                        if hasattr(vevent, 'dtend'):
                            end_dt = vevent.dtend.value
                            if not isinstance(end_dt, datetime):
                                end_dt = datetime.combine(end_dt, datetime.min.time())
                            # Naive datetime from CalDAV is typically UTC
                            if isinstance(end_dt, datetime) and end_dt.tzinfo is None:
                                end_dt = end_dt.replace(tzinfo=timezone.utc)
                            # Convert to local timezone
                            end_dt = to_local_aware(end_dt)

                        # Extract RRULE if present
                        rrule_str = None
                        if hasattr(vevent, 'rrule'):
                            rrule_str = str(vevent.rrule.value)

                        return CalendarEvent(
                            uid=str(vevent.uid.value) if hasattr(vevent, 'uid') else "",
                            summary=str(vevent.summary.value) if hasattr(vevent, 'summary') else "",
                            description=str(vevent.description.value) if hasattr(vevent, 'description') else None,
                            start=start_dt,
                            end=end_dt,
                            location=str(vevent.location.value) if hasattr(vevent, 'location') else None,
                            calendar_name="",
                            rrule=rrule_str
                        )
                except Exception:
                    pass
            except Exception:
                continue

        return None
    except Exception as e:
        logger.error(f"Failed to get event by UID: {e}")
        return None


def update_event_in_calendar(
    url: str,
    username: str,
    password: str,
    event_uid: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    location: Optional[str] = None,
    rrule: Optional[str] = None,
    user_id: Optional[int] = None,
    db: Optional[Session] = None
) -> bool:
    """Update an existing event in a CalDAV calendar.
    
    Args:
        user_id: Optional user ID for built-in mode
        db: Optional database session for built-in mode
    """
    try:
        # Check if using built-in server (direct file update)
        if password == "__USE_SESSION_AUTH__" and user_id and db:
            logger.info(f"Using built-in CalDAV storage (direct file update) for event {event_uid}")
            
            from app.models import User
            from app.services.dav_storage_proxy import DAVStorageProxy
            from icalendar import Calendar as ICalendar
            
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            
            # Use storage proxy (no local fallback)
            proxy = DAVStorageProxy(db, user.username, 'caldav')
            
            # Read existing event using proxy
            ical_data = proxy.read_file(f"{event_uid}.ics")
            if not ical_data:
                logger.warning(f"Event file not found: {event_uid}.ics")
                return False
            
            cal = ICalendar.from_ical(ical_data.encode('utf-8'))
            
            # Find and update the event component
            for component in cal.walk():
                if component.name == "VEVENT" and str(component.get('uid')) == event_uid:
                    # Update fields if provided
                    if summary is not None:
                        component['summary'] = summary
                    if description is not None:
                        component['description'] = description
                    if start_time is not None:
                        start_aware = to_local_aware(start_time)
                        start_utc = start_aware.astimezone(timezone.utc)
                        component['dtstart'] = start_utc
                    if end_time is not None:
                        end_aware = to_local_aware(end_time)
                        end_utc = end_aware.astimezone(timezone.utc)
                        component['dtend'] = end_utc
                    if location is not None:
                        component['location'] = location
                    if rrule is not None:
                        if rrule == "":
                            # Remove RRULE
                            if 'rrule' in component:
                                del component['rrule']
                        else:
                            # Parse and set RRULE
                            try:
                                rrule_dict = {}
                                for part in rrule.split(';'):
                                    if '=' in part:
                                        key, value = part.split('=', 1)
                                        if key.upper() == 'BYDAY':
                                            rrule_dict[key.lower()] = value.split(',')
                                        elif value.isdigit():
                                            rrule_dict[key.lower()] = int(value)
                                        else:
                                            rrule_dict[key.lower()] = value
                                component['rrule'] = rrule_dict
                            except Exception as e:
                                logger.warning(f"Failed to parse RRULE '{rrule}': {e}")
                    
                    # Save updated event using proxy
                    updated_ical = cal.to_ical().decode('utf-8')
                    if not proxy.write_file(f"{event_uid}.ics", updated_ical):
                        logger.error(f"Failed to save updated event {event_uid}")
                        return False
                    
                    logger.info(f"Successfully updated event {event_uid} in built-in storage")
                    return True
            
            logger.warning(f"Event component not found in file for UID {event_uid}")
            return False
        
        # Otherwise use CalDAV protocol (for external servers)
        client = create_caldav_client(url, username, password)

        try:
            calendar = caldav.Calendar(client=client, url=url)
            calendars = [calendar]
        except Exception:
            principal = client.principal()
            calendars = principal.calendars()

        for cal in calendars:
            try:
                event = cal.event_by_uid(event_uid)
                if event:
                    vevent = event.vobject_instance.vevent

                    # Update fields if provided
                    if summary is not None:
                        vevent.summary.value = summary
                    if description is not None:
                        if hasattr(vevent, 'description'):
                            vevent.description.value = description
                        else:
                            vevent.add('description').value = description
                    if start_time is not None:
                        # Convert to UTC for consistent storage (like add_event does)
                        # Use dateutil.tz.tzutc() instead of datetime.timezone.utc
                        # because vobject can't serialize Python's timezone.utc
                        from dateutil import tz as dateutil_tz
                        start_aware = to_local_aware(start_time)
                        start_utc = start_aware.astimezone(dateutil_tz.tzutc())
                        logger.info(f"Setting start_time: local={start_time} -> UTC={start_utc} (was {vevent.dtstart.value})")
                        vevent.dtstart.value = start_utc
                        # Clear TZID parameter so vobject uses Z suffix for UTC
                        if 'TZID' in vevent.dtstart.params:
                            del vevent.dtstart.params['TZID']
                        if 'X-VOBJ-ORIGINAL-TZID' in vevent.dtstart.params:
                            del vevent.dtstart.params['X-VOBJ-ORIGINAL-TZID']
                    if end_time is not None:
                        # Convert to UTC for consistent storage
                        from dateutil import tz as dateutil_tz
                        end_aware = to_local_aware(end_time)
                        end_utc = end_aware.astimezone(dateutil_tz.tzutc())
                        if hasattr(vevent, 'dtend'):
                            logger.info(f"Setting end_time: local={end_time} -> UTC={end_utc} (was {vevent.dtend.value})")
                            vevent.dtend.value = end_utc
                            # Clear TZID parameter so vobject uses Z suffix for UTC
                            if 'TZID' in vevent.dtend.params:
                                del vevent.dtend.params['TZID']
                            if 'X-VOBJ-ORIGINAL-TZID' in vevent.dtend.params:
                                del vevent.dtend.params['X-VOBJ-ORIGINAL-TZID']
                        else:
                            logger.info(f"Adding end_time: local={end_time} -> UTC={end_utc}")
                            vevent.add('dtend').value = end_utc
                    if location is not None:
                        if hasattr(vevent, 'location'):
                            vevent.location.value = location
                        else:
                            vevent.add('location').value = location

                    if rrule is not None:
                        # Update or add RRULE
                        if rrule == "":
                            # Empty string means remove recurrence
                            if hasattr(vevent, 'rrule'):
                                vevent.remove(vevent.rrule)
                                logger.info(f"Removed RRULE from event {event_uid}")
                        else:
                            # Parse and set RRULE
                            try:
                                rrule_dict = {}
                                for part in rrule.split(';'):
                                    if '=' in part:
                                        key, value = part.split('=', 1)
                                        if key.upper() == 'BYDAY':
                                            rrule_dict[key.lower()] = value.split(',')
                                        elif value.isdigit():
                                            rrule_dict[key.lower()] = int(value)
                                        else:
                                            rrule_dict[key.lower()] = value

                                if hasattr(vevent, 'rrule'):
                                    vevent.remove(vevent.rrule)
                                # Add new rrule using vobject's native method
                                vevent.add('rrule')
                                vevent.rrule.value = rrule
                                logger.info(f"Updated RRULE to: {rrule}")
                            except Exception as e:
                                logger.warning(f"Failed to parse RRULE '{rrule}': {e}")

                    # Force save by modifying the event data
                    logger.info(f"Saving event {event_uid}...")
                    event.save()
                    logger.info(f"Successfully updated event with UID: {event_uid}")
                    return True
            except Exception as e:
                logger.error(f"Error updating event {event_uid} in calendar: {e}", exc_info=True)
                continue

        logger.warning(f"Event with UID {event_uid} not found for update")
        return False

    except Exception as e:
        logger.error(f"Failed to update event: {e}", exc_info=True)
        return False


def search_contacts(
    url: str,
    username: str,
    password: str,
    query: str
) -> List[Contact]:
    """Search contacts in a CardDAV address book."""
    import requests
    from requests.auth import HTTPBasicAuth
    import re

    contacts = []
    query_lower = query.lower()

    try:
        # Use PROPFIND to list all vCard files
        headers = {
            'Content-Type': 'application/xml',
            'Depth': '1'
        }
        propfind_body = '''<?xml version="1.0" encoding="utf-8" ?>
        <D:propfind xmlns:D="DAV:">
          <D:prop>
            <D:getcontenttype/>
            <D:getetag/>
          </D:prop>
        </D:propfind>'''

        resp = requests.request('PROPFIND', url, auth=HTTPBasicAuth(username, password),
                               headers=headers, data=propfind_body, timeout=30, verify=False)

        if resp.status_code != 207:
            logger.error(f"CardDAV PROPFIND failed: {resp.status_code}")
            return contacts

        # Parse response to find .vcf files
        vcf_urls = re.findall(r'<href>([^<]+\.vcf)</href>', resp.text)
        logger.debug(f"Found {len(vcf_urls)} vCard files")

        # Fetch each vCard
        base_url = url.rstrip('/')
        # Extract base (remove path after username)
        url_parts = url.split('/')
        scheme_host = '/'.join(url_parts[:3])  # https://cal.poster.place

        for vcf_path in vcf_urls:
            try:
                # Build full URL
                if vcf_path.startswith('http'):
                    vcf_url = vcf_path
                elif vcf_path.startswith('/'):
                    vcf_url = scheme_host + vcf_path
                else:
                    vcf_url = base_url + '/' + vcf_path

                vcf_resp = requests.get(vcf_url, auth=HTTPBasicAuth(username, password), timeout=10, verify=False)
                if vcf_resp.status_code != 200:
                    continue

                vcard_data = vcf_resp.text
                vcard = vobject.readOne(vcard_data)

                # Get contact info
                name = ""
                if hasattr(vcard, 'fn'):
                    name = str(vcard.fn.value)
                elif hasattr(vcard, 'n'):
                    name = str(vcard.n.value)

                emails = []
                # Collect all email addresses from vcard
                if hasattr(vcard, 'email_list'):
                    for em in vcard.email_list:
                        email_val = str(em.value).strip()
                        if email_val and email_val not in emails:
                            emails.append(email_val)
                elif hasattr(vcard, 'email'):
                    email_val = str(vcard.email.value).strip()
                    if email_val:
                        emails.append(email_val)

                phone = None
                if hasattr(vcard, 'tel'):
                    phone = str(vcard.tel.value)

                org = None
                if hasattr(vcard, 'org'):
                    org = str(vcard.org.value[0]) if vcard.org.value else None

                # Extract address from ADR field (vCard format: ;;;street;city;state;postal;country)
                address = None
                if hasattr(vcard, 'adr'):
                    # Handle both single ADR object and list of ADR objects
                    adr_objects = []
                    if isinstance(vcard.adr, list):
                        adr_objects = vcard.adr
                    else:
                        adr_objects = [vcard.adr]
                    
                    # Use the first ADR object
                    if adr_objects:
                        adr = adr_objects[0].value
                        if isinstance(adr, list) and len(adr) >= 4:
                            # ADR format: [post_office_box, extended, street, city, state, postal_code, country]
                            address_parts = []
                            if len(adr) > 2 and adr[2]:  # street
                                address_parts.append(str(adr[2]))
                            if len(adr) > 3 and adr[3]:  # city
                                address_parts.append(str(adr[3]))
                            if len(adr) > 4 and adr[4]:  # state
                                address_parts.append(str(adr[4]))
                            if len(adr) > 5 and adr[5]:  # postal_code
                                address_parts.append(str(adr[5]))
                            if len(adr) > 6 and adr[6]:  # country
                                address_parts.append(str(adr[6]))
                            if address_parts:
                                address = ', '.join(address_parts)

                note = None
                if hasattr(vcard, 'note'):
                    note = str(vcard.note.value)

                # If no email found, try to extract from note field
                if not emails and note:
                    # Try "EMail (preferred) : email@example.com" format first
                    email_label_matches = re.findall(r'EMail[^:]*:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', note, re.IGNORECASE)
                    if email_label_matches:
                        for em in email_label_matches:
                            if em not in emails:
                                emails.append(em)
                    else:
                        # Fall back to any email pattern in note
                        email_matches = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', note)
                        for em in email_matches:
                            if em not in emails:
                                emails.append(em)

                # Check if query matches (empty query matches all)
                emails_str = ' '.join(emails)
                searchable = f"{name} {emails_str} {phone or ''} {org or ''} {address or ''} {note or ''}".lower()
                if not query_lower or query_lower in searchable:
                    contacts.append(Contact(
                        uid=str(vcard.uid.value) if hasattr(vcard, 'uid') else "",
                        name=name,
                        emails=emails,
                        phone=phone,
                        organization=org,
                        address=address,
                        note=note
                    ))

            except Exception as e:
                logger.debug(f"Error parsing vCard {vcf_path}: {e}")
                continue

    except Exception as e:
        logger.error(f"Failed to search contacts: {e}")

    return contacts


def get_user_contacts(user_id: int, query: str, db: Session = None) -> List[Contact]:
    """Search contacts using user's CardDAV configuration with timeout protection. Uses storage proxy for built-in server."""
    config = get_user_contacts_config(user_id, db)
    if not config:
        logger.debug(f"[Contacts] No CardDAV config found for user {user_id}")
        return []

    url = config.get('url', '')
    username = config.get('username', '')
    password = config.get('password', '')
    is_builtin = config.get('builtin', False)

    logger.debug(f"[Contacts] Config for user {user_id}: url={url}, username={username}, is_builtin={is_builtin}, password={password[:20] if password else 'None'}...")

    if not url or not username:
        logger.debug(f"[Contacts] Missing url or username for user {user_id}")
        return []

    # If using built-in server, read directly from storage proxy
    if is_builtin and password == "__USE_SESSION_AUTH__":
        logger.debug(f"[Contacts] Using built-in CardDAV server for user {user_id}")
        try:
            from app.models import User
            from app.services.dav_storage_proxy import DAVStorageProxy
            import vobject
            
            if db is None:
                db = SessionLocal()
            
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return []
            
            # Use storage proxy (must be configured)
            proxy = DAVStorageProxy(db, user.username, 'cardav')
            
            # Search all addressbook subdirectories and root
            root_items = proxy.list_files("")
            addressbook_dirs = []
            for item in root_items:
                if item.get('is_directory', False) and not item.get('name', '').startswith('.'):
                    addressbook_dirs.append(item.get('name'))
            
            contacts = []
            query_lower = query.lower()
            
            # Search all addressbook subdirectories
            for addr_dir in addressbook_dirs:
                file_items = proxy.list_files(addr_dir)
                for item in file_items:
                    name = item.get('name', '')
                    if not name.endswith('.vcf'):
                        continue
                    
                    try:
                        # Read vCard file using proxy
                        filepath = f"{addr_dir}/{name}" if addr_dir else name
                        vcard_data = proxy.read_file(filepath)
                        if not vcard_data:
                            continue
                        
                        vcard = vobject.readOne(vcard_data)
                        
                        # Get contact info
                        contact_name = ""
                        if hasattr(vcard, 'fn'):
                            contact_name = str(vcard.fn.value)
                        elif hasattr(vcard, 'n'):
                            contact_name = str(vcard.n.value)
                        
                        emails = []
                        if hasattr(vcard, 'email_list'):
                            for em in vcard.email_list:
                                email_val = str(em.value).strip()
                                if email_val and email_val not in emails:
                                    emails.append(email_val)
                        elif hasattr(vcard, 'email'):
                            email_val = str(vcard.email.value).strip()
                            if email_val:
                                emails.append(email_val)
                        
                        phone = None
                        if hasattr(vcard, 'tel'):
                            phone = str(vcard.tel.value)
                        
                        org = None
                        if hasattr(vcard, 'org'):
                            org = str(vcard.org.value[0]) if vcard.org.value else None
                        
                        # Extract address from ADR field
                        address = None
                        if hasattr(vcard, 'adr'):
                            # Handle both single ADR object and list of ADR objects
                            adr_objects = []
                            if isinstance(vcard.adr, list):
                                adr_objects = vcard.adr
                            else:
                                adr_objects = [vcard.adr]
                            
                            # Use the first ADR object
                            if adr_objects:
                                adr = adr_objects[0].value
                                if isinstance(adr, list) and len(adr) >= 4:
                                    address_parts = []
                                    if len(adr) > 2 and adr[2]:  # street
                                        address_parts.append(str(adr[2]))
                                    if len(adr) > 3 and adr[3]:  # city
                                        address_parts.append(str(adr[3]))
                                    if len(adr) > 4 and adr[4]:  # state
                                        address_parts.append(str(adr[4]))
                                    if len(adr) > 5 and adr[5]:  # postal_code
                                        address_parts.append(str(adr[5]))
                                    if len(adr) > 6 and adr[6]:  # country
                                        address_parts.append(str(adr[6]))
                                    if address_parts:
                                        address = ', '.join(address_parts)
                        
                        note = None
                        if hasattr(vcard, 'note'):
                            note = str(vcard.note.value)
                        
                        # Check if query matches (empty query matches all)
                        emails_str = ' '.join(emails)
                        searchable = f"{contact_name} {emails_str} {phone or ''} {org or ''} {address or ''} {note or ''}".lower()
                        if not query_lower or query_lower in searchable:
                            contacts.append(Contact(
                                uid=str(vcard.uid.value) if hasattr(vcard, 'uid') else name.replace('.vcf', ''),
                                name=contact_name,
                                emails=emails,
                                phone=phone,
                                organization=org,
                                address=address,
                                note=note
                            ))
                    except Exception as e:
                        logger.debug(f"Error parsing vCard {name}: {e}")
                        continue
            
            # Also check root for legacy .vcf files
            root_file_items = proxy.list_files("")
            for item in root_file_items:
                name = item.get('name', '')
                if not name.endswith('.vcf'):
                    continue
                
                try:
                    # Read vCard file using proxy
                    vcard_data = proxy.read_file(name)
                    if not vcard_data:
                        continue
                    
                    vcard = vobject.readOne(vcard_data)
                    
                    # Get contact info
                    contact_name = ""
                    if hasattr(vcard, 'fn'):
                        contact_name = str(vcard.fn.value)
                    elif hasattr(vcard, 'n'):
                        contact_name = str(vcard.n.value)
                    
                    emails = []
                    if hasattr(vcard, 'email_list'):
                        for em in vcard.email_list:
                            email_val = str(em.value).strip()
                            if email_val and email_val not in emails:
                                emails.append(email_val)
                    elif hasattr(vcard, 'email'):
                        email_val = str(vcard.email.value).strip()
                        if email_val:
                            emails.append(email_val)
                    
                    phone = None
                    if hasattr(vcard, 'tel'):
                        phone = str(vcard.tel.value)
                    
                        org = None
                        if hasattr(vcard, 'org'):
                            org = str(vcard.org.value[0]) if vcard.org.value else None
                        
                        # Extract address from ADR field
                        address = None
                        if hasattr(vcard, 'adr'):
                            # Handle both single ADR object and list of ADR objects
                            adr_objects = []
                            if isinstance(vcard.adr, list):
                                adr_objects = vcard.adr
                            else:
                                adr_objects = [vcard.adr]
                            
                            # Use the first ADR object
                            if adr_objects:
                                adr = adr_objects[0].value
                                if isinstance(adr, list) and len(adr) >= 4:
                                    address_parts = []
                                    if len(adr) > 2 and adr[2]:  # street
                                        address_parts.append(str(adr[2]))
                                    if len(adr) > 3 and adr[3]:  # city
                                        address_parts.append(str(adr[3]))
                                    if len(adr) > 4 and adr[4]:  # state
                                        address_parts.append(str(adr[4]))
                                    if len(adr) > 5 and adr[5]:  # postal_code
                                        address_parts.append(str(adr[5]))
                                    if len(adr) > 6 and adr[6]:  # country
                                        address_parts.append(str(adr[6]))
                                    if address_parts:
                                        address = ', '.join(address_parts)
                        
                        note = None
                        if hasattr(vcard, 'note'):
                            note = str(vcard.note.value)
                        
                        # Check if query matches (empty query matches all)
                        emails_str = ' '.join(emails)
                        searchable = f"{contact_name} {emails_str} {phone or ''} {org or ''} {address or ''} {note or ''}".lower()
                        if not query_lower or query_lower in searchable:
                            contacts.append(Contact(
                                uid=str(vcard.uid.value) if hasattr(vcard, 'uid') else name.replace('.vcf', ''),
                                name=contact_name,
                                emails=emails,
                                phone=phone,
                                organization=org,
                                address=address,
                                note=note
                            ))
                except Exception as e:
                    logger.debug(f"Error parsing vCard {name}: {e}")
                    continue
            
            return contacts
        except Exception as e:
            logger.error(f"Error fetching contacts from built-in storage: {e}", exc_info=True)
            return []

    # External CardDAV server - use HTTP requests
    try:
        # Run with timeout to prevent hanging on slow CardDAV servers
        def fetch_contacts():
            return search_contacts(url, username, password, query)

        contacts = run_with_timeout(fetch_contacts, timeout=CALDAV_OPERATION_TIMEOUT)
        return contacts if contacts else []
    except Exception as e:
        logger.error(f"Error fetching contacts: {e}")
        return []


def add_contact(
    url: str,
    username: str,
    password: str,
    name: str,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    organization: Optional[str] = None,
    note: Optional[str] = None
) -> bool:
    """Add a new contact to CardDAV address book."""
    import uuid
    import re
    import requests

    try:
        logger.info(f"Adding contact '{name}' to {url}")

        # Build vCard
        uid = str(uuid.uuid4())
        vcard_lines = [
            "BEGIN:VCARD",
            "VERSION:3.0",
            f"UID:{uid}",
            f"FN:{name}",
        ]

        # Parse name into N field (last;first;middle;prefix;suffix)
        name_parts = name.split()
        if len(name_parts) >= 2:
            vcard_lines.append(f"N:{name_parts[-1]};{' '.join(name_parts[:-1])};;;")
        else:
            vcard_lines.append(f"N:{name};;;;")

        if phone:
            # Clean phone number
            clean_phone = re.sub(r'[^\d+]', '', phone)
            vcard_lines.append(f"TEL;TYPE=CELL:{phone}")

        if email:
            vcard_lines.append(f"EMAIL;TYPE=INTERNET:{email}")

        if organization:
            vcard_lines.append(f"ORG:{organization}")

        if note:
            vcard_lines.append(f"NOTE:{note}")

        vcard_lines.append("END:VCARD")
        vcard_data = "\r\n".join(vcard_lines)

        # Upload to CardDAV
        vcf_url = f"{url.rstrip('/')}/{uid}.vcf"

        response = requests.put(
            vcf_url,
            data=vcard_data,
            auth=(username, password),
            headers={
                "Content-Type": "text/vcard; charset=utf-8",
            },
            timeout=30,
            verify=False
        )

        if response.status_code in (200, 201, 204):
            logger.info(f"Contact '{name}' added successfully")
            return True
        else:
            logger.error(f"Failed to add contact: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"Error adding contact: {e}")
        return False


def add_user_contact(
    user_id: int,
    db: Session,
    name: str,
    phone: Optional[str] = None,
    email: Optional[str] = None
) -> bool:
    """Add a new contact using user's CardDAV configuration."""
    config = get_user_contacts_config(user_id, db)
    if not config:
        return False

    url = config.get('url', '')
    username = config.get('username', '')
    password = config.get('password', '')

    if not url or not username:
        return False
    
    # Check if using built-in server (direct file save)
    if password == "__USE_SESSION_AUTH__" and config.get('builtin'):
        import uuid
        uid = str(uuid.uuid4())
        vcard_lines = [
            "BEGIN:VCARD",
            "VERSION:3.0",
            f"UID:{uid}",
            f"FN:{name}"
        ]
        
        # Add name components
        name_parts = name.split()
        if len(name_parts) > 1:
            vcard_lines.append(f"N:{name_parts[-1]};{' '.join(name_parts[:-1])};;;")
        else:
            vcard_lines.append(f"N:{name};;;;")
        
        if phone:
            vcard_lines.append(f"TEL;TYPE=CELL:{phone}")
        if email:
            vcard_lines.append(f"EMAIL;TYPE=INTERNET:{email}")
        
        vcard_lines.append("END:VCARD")
        vcard_data = "\r\n".join(vcard_lines)
        
        return _save_contact_to_builtin(user_id, db, vcard_data)

    return add_contact(url, username, password, name, phone, email)


def delete_contact(url: str, username: str, password: str, contact_uid: str) -> bool:
    """Delete a contact (vCard) from CardDAV addressbook by UID."""
    import requests
    from requests.auth import HTTPBasicAuth
    import re

    try:
        # Use PROPFIND to list all vCard files
        headers = {
            'Content-Type': 'application/xml',
            'Depth': '1'
        }
        propfind_body = '''<?xml version="1.0" encoding="utf-8" ?>
        <D:propfind xmlns:D="DAV:">
          <D:prop>
            <D:getcontenttype/>
            <D:getetag/>
          </D:prop>
        </D:propfind>'''

        resp = requests.request('PROPFIND', url, auth=HTTPBasicAuth(username, password),
                               headers=headers, data=propfind_body, timeout=30, verify=False)

        if resp.status_code != 207:
            logger.error(f"CardDAV PROPFIND failed: {resp.status_code}")
            return False

        # Parse response to find .vcf files
        vcf_urls = re.findall(r'<href>([^<]+\.vcf)</href>', resp.text)

        # Build base URL
        base_url = url.rstrip('/')
        url_parts = url.split('/')
        scheme_host = '/'.join(url_parts[:3])

        for vcf_path in vcf_urls:
            try:
                # Build full URL
                if vcf_path.startswith('http'):
                    vcf_url = vcf_path
                elif vcf_path.startswith('/'):
                    vcf_url = scheme_host + vcf_path
                else:
                    vcf_url = base_url + '/' + vcf_path

                vcf_resp = requests.get(vcf_url, auth=HTTPBasicAuth(username, password), timeout=10, verify=False)
                if vcf_resp.status_code != 200:
                    continue

                vcard_data = vcf_resp.text
                vcard = vobject.readOne(vcard_data)

                # Check if UID matches
                vcard_uid = str(vcard.uid.value) if hasattr(vcard, 'uid') else None
                if vcard_uid == contact_uid:
                    # Delete the vCard
                    delete_resp = requests.delete(vcf_url, auth=HTTPBasicAuth(username, password), timeout=10, verify=False)
                    if delete_resp.status_code in (200, 204, 404):  # 404 means already deleted
                        logger.info(f"Deleted contact with UID: {contact_uid}")
                        return True
                    else:
                        logger.error(f"Failed to delete contact: HTTP {delete_resp.status_code}")
                        return False

            except Exception as e:
                logger.debug(f"Error processing vCard: {e}")
                continue

        logger.warning(f"Contact not found with UID: {contact_uid}")
        return False
    except Exception as e:
        logger.error(f"Failed to delete contact: {e}")
        return False


def edit_contact(url: str, username: str, password: str, contact_uid: str, updates: dict) -> bool:
    """Edit a contact (vCard) in CardDAV addressbook by UID.

    Args:
        url: CardDAV addressbook URL
        username: Username for authentication
        password: Password for authentication
        contact_uid: UID of the contact to edit
        updates: Dictionary with fields to update: {'name': 'New Name', 'phone': '555-1234', 'email': 'test@example.com', ...}

    Returns:
        True if successful, False otherwise
    """
    import requests
    from requests.auth import HTTPBasicAuth
    import re

    logger.info(f"edit_contact called: url={url}, uid={contact_uid}, updates={updates}")

    try:
        # Use PROPFIND to list all vCard files
        headers = {
            'Content-Type': 'application/xml',
            'Depth': '1'
        }
        propfind_body = '''<?xml version="1.0" encoding="utf-8" ?>
        <D:propfind xmlns:D="DAV:">
          <D:prop>
            <D:getcontenttype/>
            <D:getetag/>
          </D:prop>
        </D:propfind>'''

        resp = requests.request('PROPFIND', url, auth=HTTPBasicAuth(username, password),
                               headers=headers, data=propfind_body, timeout=30, verify=False)

        if resp.status_code != 207:
            logger.error(f"CardDAV PROPFIND failed: {resp.status_code}")
            return False

        # Parse response to find .vcf files
        vcf_urls = re.findall(r'<href>([^<]+\.vcf)</href>', resp.text)

        # Build base URL
        base_url = url.rstrip('/')
        url_parts = url.split('/')
        scheme_host = '/'.join(url_parts[:3])

        for vcf_path in vcf_urls:
            try:
                # Build full URL
                if vcf_path.startswith('http'):
                    vcf_url = vcf_path
                elif vcf_path.startswith('/'):
                    vcf_url = scheme_host + vcf_path
                else:
                    vcf_url = base_url + '/' + vcf_path

                vcf_resp = requests.get(vcf_url, auth=HTTPBasicAuth(username, password), timeout=10, verify=False)
                if vcf_resp.status_code != 200:
                    continue

                # Capture ETag for conditional update (required by many CardDAV servers)
                contact_etag = vcf_resp.headers.get('ETag', '')
                
                vcard_data = vcf_resp.text
                vcard = vobject.readOne(vcard_data)

                # Check if UID matches
                vcard_uid = str(vcard.uid.value) if hasattr(vcard, 'uid') else None
                if vcard_uid == contact_uid:
                    # Update the vCard fields
                    if 'name' in updates and updates['name']:
                        # Update FN (formatted name)
                        if hasattr(vcard, 'fn'):
                            vcard.fn.value = updates['name']
                        else:
                            vcard.add('fn')
                            vcard.fn.value = updates['name']
                        # Update N (structured name)
                        parts = updates['name'].split(' ', 1)
                        if hasattr(vcard, 'n'):
                            vcard.n.value = vobject.vcard.Name(family=parts[-1], given=parts[0] if len(parts) > 1 else '')
                        else:
                            vcard.add('n')
                            vcard.n.value = vobject.vcard.Name(family=parts[-1], given=parts[0] if len(parts) > 1 else '')

                    if 'phone' in updates:
                        # Remove existing phone
                        if hasattr(vcard, 'tel'):
                            del vcard.tel
                        # Add new phone if not empty
                        if updates['phone']:
                            vcard.add('tel')
                            vcard.tel.value = updates['phone']
                            vcard.tel.type_param = 'CELL'

                    if 'email' in updates:
                        # Remove existing email
                        if hasattr(vcard, 'email'):
                            del vcard.email
                        # Add new email if not empty
                        if updates['email']:
                            vcard.add('email')
                            vcard.email.value = updates['email']
                            vcard.email.type_param = 'INTERNET'

                    if 'organization' in updates:
                        if updates.get('organization'):
                            if not hasattr(vcard, 'org'):
                                vcard.add('org')
                            vcard.org.value = [updates['organization']]
                        elif hasattr(vcard, 'org'):
                            del vcard.org

                    if 'address' in updates:
                        address_str = updates.get('address', '').strip()
                        if address_str:
                            # Parse address into ADR components
                            address_parts = address_str.split(',')
                            if len(address_parts) >= 2:
                                street = address_parts[0].strip()
                                city = address_parts[1].strip() if len(address_parts) > 1 else ''
                                state = address_parts[2].strip() if len(address_parts) > 2 else ''
                                postal = address_parts[3].strip() if len(address_parts) > 3 else ''
                                country = address_parts[4].strip() if len(address_parts) > 4 else ''
                                # If state contains zip, split it
                                if state and ' ' in state:
                                    state_parts = state.rsplit(' ', 1)
                                    state = state_parts[0]
                                    postal = state_parts[1] if len(state_parts) > 1 else postal
                            else:
                                street = address_str
                                city = state = postal = country = ''
                            
                            # Create proper vobject Address object
                            new_adr = vobject.vcard.Address(
                                street=street,
                                city=city,
                                region=state,
                                code=postal,
                                country=country,
                                box='',
                                extended=''
                            )
                            
                            # Remove existing ADR fields first
                            if hasattr(vcard, 'adr'):
                                if isinstance(vcard.adr, list):
                                    for adr in list(vcard.adr):
                                        vcard.remove(adr)
                                else:
                                    vcard.remove(vcard.adr)
                            
                            # Add new address - after removing all, add() should create a single property
                            vcard.add('adr')
                            # Access the newly added property (should be single, not list, since we removed all)
                            if hasattr(vcard, 'adr'):
                                if isinstance(vcard.adr, list):
                                    # If still a list (shouldn't happen after removal), use first element
                                    adr_prop = vcard.adr[0] if vcard.adr else None
                                else:
                                    adr_prop = vcard.adr
                                
                                if adr_prop:
                                    adr_prop.value = new_adr
                                    adr_prop.type_param = 'HOME'
                        elif hasattr(vcard, 'adr'):
                            # Remove address if empty - handle list case
                            if isinstance(vcard.adr, list):
                                for adr in list(vcard.adr):
                                    vcard.remove(adr)
                            else:
                                vcard.remove(vcard.adr)

                    if 'note' in updates:
                        if updates.get('note'):
                            if not hasattr(vcard, 'note'):
                                vcard.add('note')
                            vcard.note.value = updates['note']
                        elif hasattr(vcard, 'note'):
                            del vcard.note

                    # Update REV timestamp
                    from datetime import datetime, timezone
                    if hasattr(vcard, 'rev'):
                        vcard.rev.value = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                    else:
                        vcard.add('rev')
                        vcard.rev.value = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

                    # Serialize and PUT back
                    updated_vcard_data = vcard.serialize()
                    put_headers = {
                        'Content-Type': 'text/vcard; charset=utf-8'
                    }
                    # Add If-Match header if we have an ETag (required by many CardDAV servers)
                    if contact_etag:
                        put_headers['If-Match'] = contact_etag
                    
                    put_resp = requests.put(vcf_url, auth=HTTPBasicAuth(username, password),
                                          headers=put_headers, data=updated_vcard_data, timeout=10, verify=False)

                    if put_resp.status_code in (200, 201, 204):
                        logger.info(f"Updated contact with UID: {contact_uid}")
                        return True
                    else:
                        logger.error(f"Failed to update contact: HTTP {put_resp.status_code}, Response: {put_resp.text[:500]}")
                        return False

            except Exception as e:
                logger.error(f"Error processing vCard: {e}", exc_info=True)
                continue

        logger.warning(f"Contact not found with UID: {contact_uid}")
        return False
    except Exception as e:
        logger.error(f"Failed to edit contact: {e}", exc_info=True)
        return False


def edit_user_contact(user_id: int, db: Session, contact_uid: str, updates: dict) -> bool:
    """Edit a contact using user's CardDAV configuration."""
    config = get_user_contacts_config(user_id, db)
    if not config:
        return False

    url = config.get('url', '')
    username = config.get('username', '')
    password = config.get('password', '')

    if url and username:
        # Check if using built-in server (direct file update)
        if password == "__USE_SESSION_AUTH__" and config.get('builtin'):
            return _edit_contact_builtin(user_id, db, contact_uid, updates)
        return edit_contact(url, username, password, contact_uid, updates)
    return False


def delete_user_contact(user_id: int, db: Session, contact_uid: str) -> bool:
    """Delete a contact using user's CardDAV configuration."""
    config = get_user_contacts_config(user_id, db)
    if not config:
        return False

    url = config.get('url', '')
    username = config.get('username', '')
    password = config.get('password', '')

    if not url or not username:
        return False
    
    # Check if using built-in server (direct file delete)
    if password == "__USE_SESSION_AUTH__" and config.get('builtin'):
        return _delete_contact_builtin(user_id, db, contact_uid)

    return delete_contact(url, username, password, contact_uid)


def get_contact_by_uid(url: str, username: str, password: str, contact_uid: str) -> Optional[Contact]:
    """Get a specific contact by UID."""
    import requests
    from requests.auth import HTTPBasicAuth
    import re

    try:
        # Use PROPFIND to list all vCard files
        headers = {
            'Content-Type': 'application/xml',
            'Depth': '1'
        }
        propfind_body = '''<?xml version="1.0" encoding="utf-8" ?>
        <D:propfind xmlns:D="DAV:">
          <D:prop>
            <D:getcontenttype/>
            <D:getetag/>
          </D:prop>
        </D:propfind>'''

        resp = requests.request('PROPFIND', url, auth=HTTPBasicAuth(username, password),
                               headers=headers, data=propfind_body, timeout=30, verify=False)

        if resp.status_code != 207:
            logger.error(f"CardDAV PROPFIND failed: {resp.status_code}")
            return None

        # Parse response to find .vcf files
        vcf_urls = re.findall(r'<href>([^<]+\.vcf)</href>', resp.text)

        # Build base URL
        base_url = url.rstrip('/')
        url_parts = url.split('/')
        scheme_host = '/'.join(url_parts[:3])

        for vcf_path in vcf_urls:
            try:
                # Build full URL
                if vcf_path.startswith('http'):
                    vcf_url = vcf_path
                elif vcf_path.startswith('/'):
                    vcf_url = scheme_host + vcf_path
                else:
                    vcf_url = base_url + '/' + vcf_path

                vcf_resp = requests.get(vcf_url, auth=HTTPBasicAuth(username, password), timeout=10, verify=False)
                if vcf_resp.status_code != 200:
                    continue

                vcard_data = vcf_resp.text
                vcard = vobject.readOne(vcard_data)

                # Check if UID matches
                vcard_uid = str(vcard.uid.value) if hasattr(vcard, 'uid') else None
                if vcard_uid == contact_uid:
                    # Extract contact info
                    name = ""
                    if hasattr(vcard, 'fn'):
                        name = str(vcard.fn.value)
                    elif hasattr(vcard, 'n'):
                        name = str(vcard.n.value)

                    emails = []
                    if hasattr(vcard, 'email_list'):
                        for em in vcard.email_list:
                            email_val = str(em.value).strip()
                            if email_val and email_val not in emails:
                                emails.append(email_val)
                    elif hasattr(vcard, 'email'):
                        email_val = str(vcard.email.value).strip()
                        if email_val:
                            emails.append(email_val)

                    phone = None
                    if hasattr(vcard, 'tel'):
                        phone = str(vcard.tel.value)

                    org = None
                    if hasattr(vcard, 'org'):
                        org = str(vcard.org.value[0]) if vcard.org.value else None

                    # Extract address from ADR field
                    address = None
                    if hasattr(vcard, 'adr'):
                        # Handle both list and single ADR objects
                        adr_obj = vcard.adr[0] if isinstance(vcard.adr, list) and len(vcard.adr) > 0 else vcard.adr
                        adr = adr_obj.value if hasattr(adr_obj, 'value') else None
                        if adr and isinstance(adr, list) and len(adr) >= 4:
                            address_parts = []
                            if len(adr) > 2 and adr[2]:  # street
                                address_parts.append(str(adr[2]))
                            if len(adr) > 3 and adr[3]:  # city
                                address_parts.append(str(adr[3]))
                            if len(adr) > 4 and adr[4]:  # state
                                address_parts.append(str(adr[4]))
                            if len(adr) > 5 and adr[5]:  # postal_code
                                address_parts.append(str(adr[5]))
                            if len(adr) > 6 and adr[6]:  # country
                                address_parts.append(str(adr[6]))
                            if address_parts:
                                address = ', '.join(address_parts)

                    note = None
                    if hasattr(vcard, 'note'):
                        note = str(vcard.note.value)

                    return Contact(
                        uid=contact_uid,
                        name=name,
                        emails=emails,
                        phone=phone,
                        organization=org,
                        address=address,
                        note=note
                    )

            except Exception as e:
                logger.debug(f"Error processing vCard: {e}")
                continue

        logger.warning(f"Contact not found with UID: {contact_uid}")
        return None
    except Exception as e:
        logger.error(f"Failed to get contact: {e}")
        return None


def get_user_contact_by_uid(user_id: int, db: Session, contact_uid: str) -> Optional[Contact]:
    """Get a contact using user's CardDAV configuration. Uses storage proxy for built-in server."""
    config = get_user_contacts_config(user_id, db)
    if not config:
        return None

    url = config.get('url', '')
    username = config.get('username', '')
    password = config.get('password', '')
    is_builtin = config.get('builtin', False)

    if not url or not username:
        return None

    # If using built-in server, read directly from storage proxy
    if is_builtin and password == "__USE_SESSION_AUTH__":
        try:
            from app.models import User
            from app.services.dav_storage_proxy import DAVStorageProxy
            import vobject
            
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return None
            
            # Use storage proxy (must be configured)
            proxy = DAVStorageProxy(db, user.username, 'cardav')
            
            # Try to find contact by UID - check all .vcf files
            file_items = proxy.list_files("")
            
            for item in file_items:
                name = item.get('name', '')
                if not name.endswith('.vcf'):
                    continue
                
                try:
                    # Read vCard file using proxy
                    vcard_data = proxy.read_file(name)
                    if not vcard_data:
                        continue
                    
                    vcard = vobject.readOne(vcard_data)
                    vcard_uid = str(vcard.uid.value) if hasattr(vcard, 'uid') else name.replace('.vcf', '')
                    
                    if vcard_uid == contact_uid:
                        # Found the contact - parse and return
                        contact_name = ""
                        if hasattr(vcard, 'fn'):
                            contact_name = str(vcard.fn.value)
                        elif hasattr(vcard, 'n'):
                            contact_name = str(vcard.n.value)
                        
                        emails = []
                        if hasattr(vcard, 'email_list'):
                            for em in vcard.email_list:
                                email_val = str(em.value).strip()
                                if email_val and email_val not in emails:
                                    emails.append(email_val)
                        elif hasattr(vcard, 'email'):
                            email_val = str(vcard.email.value).strip()
                            if email_val:
                                emails.append(email_val)
                        
                        phone = None
                        if hasattr(vcard, 'tel'):
                            phone = str(vcard.tel.value)
                        
                        org = None
                        if hasattr(vcard, 'org'):
                            org = str(vcard.org.value[0]) if vcard.org.value else None
                        
                        # Extract address from ADR field
                        address = None
                        if hasattr(vcard, 'adr'):
                            # Handle both single ADR object and list of ADR objects
                            adr_objects = []
                            if isinstance(vcard.adr, list):
                                adr_objects = vcard.adr
                            else:
                                adr_objects = [vcard.adr]
                            
                            # Use the first ADR object
                            if adr_objects:
                                adr = adr_objects[0].value
                                if isinstance(adr, list) and len(adr) >= 4:
                                    address_parts = []
                                    if len(adr) > 2 and adr[2]:  # street
                                        address_parts.append(str(adr[2]))
                                    if len(adr) > 3 and adr[3]:  # city
                                        address_parts.append(str(adr[3]))
                                    if len(adr) > 4 and adr[4]:  # state
                                        address_parts.append(str(adr[4]))
                                    if len(adr) > 5 and adr[5]:  # postal_code
                                        address_parts.append(str(adr[5]))
                                    if len(adr) > 6 and adr[6]:  # country
                                        address_parts.append(str(adr[6]))
                                    if address_parts:
                                        address = ', '.join(address_parts)
                        
                        note = None
                        if hasattr(vcard, 'note'):
                            note = str(vcard.note.value)
                        
                        return Contact(
                            uid=vcard_uid,
                            name=contact_name,
                            emails=emails,
                            phone=phone,
                            organization=org,
                            address=address,
                            note=note
                        )
                except Exception as e:
                    logger.debug(f"Error parsing vCard {name}: {e}")
                    continue
            
            return None
        except Exception as e:
            logger.error(f"Error getting contact from built-in storage: {e}", exc_info=True)
            return None

    # External CardDAV server - use HTTP requests
    return get_contact_by_uid(url, username, password, contact_uid)


def format_events_for_display(events: List[CalendarEvent], include_description: bool = False, cyberpunk: bool = False) -> str:
    """Format events for display with clickable map links for locations and delete buttons.
    
    Events are grouped by date, and calendar names are shown for each event when available.
    """
    import urllib.parse

    if not events:
        if cyberpunk:
            return "📅 No events scheduled.\n\n[➕ Add Event](cmd:cal add )"
        return "No events found. [Add Event](cmd:cal add )"

    lines = []
    current_date = None
    first_event_of_day = True

    # Sort events by start time
    sorted_events = sorted(events, key=lambda e: e.start)

    for event in sorted_events:
        event_date = event.start.date()
        if event_date != current_date:
            current_date = event_date
            first_event_of_day = True
            if cyberpunk:
                # Cyberpunk style date header
                day_abbrev = event_date.strftime('%a').upper()
                date_formatted = event_date.strftime('%b %d')
                lines.append(f"\n**[{day_abbrev}]** {date_formatted}")
            else:
                lines.append(f"\n**{event_date.strftime('%A, %B %d, %Y')}**")
        else:
            # Add spacing between events on same day
            if not first_event_of_day:
                lines.append("")  # Blank line for spacing

        first_event_of_day = False

        time_str = event.start.strftime("%I:%M %p").lstrip('0')
        if event.end:
            end_str = event.end.strftime("%I:%M %p").lstrip('0')
            time_str = f"{time_str} - {end_str}"

        # Build action links if event has UID
        action_links = ""
        if event.uid:
            # Use cmd: links for TUI compatibility
            edit_cmd = f"cal get {event.uid}"
            delete_cmd = f"cal delete {event.uid}"
            action_links = f" [✏️](cmd:{edit_cmd}) [🗑️](cmd:{delete_cmd})"

        if cyberpunk:
            # Cyberpunk style event line with time in brackets
            time_bracket = event.start.strftime("%H:%M")
            # Show calendar name if available and not generic
            calendar_label = ""
            if event.calendar_name and event.calendar_name not in ("Built-in Calendar", "Calendar"):
                calendar_label = f" `[{event.calendar_name}]`"
            line = f"  ⏰ `{time_bracket}` **{event.summary}**{calendar_label}{action_links}"
        else:
            # Show calendar name if available and not generic
            calendar_label = ""
            if event.calendar_name and event.calendar_name not in ("Built-in Calendar", "Calendar"):
                calendar_label = f" ({event.calendar_name})"
            line = f"- {time_str}: {event.summary}{calendar_label}{action_links}"

        if event.location:
            # Create Google Maps link for mobile
            maps_url = f"https://maps.google.com/maps?q={urllib.parse.quote(event.location)}"
            if cyberpunk:
                line += f"\n    📍 [{event.location}]({maps_url})"
            else:
                line += f" @ [{event.location}]({maps_url})"
        lines.append(line)

        if include_description and event.description:
            if cyberpunk:
                lines.append(f"    _{event.description}_")
            else:
                lines.append(f"  _{event.description}_")

    return "\n".join(lines)


def format_contacts_for_display(contacts: List[Contact]) -> str:
    """Format contacts for display with clickable phone and email links and action buttons."""
    import re

    if not contacts:
        return "No contacts found."

    def format_phone_link(phone: str) -> str:
        """Create tel: link from phone number."""
        # Remove all non-digit characters except + for international
        clean = re.sub(r'[^\d+]', '', phone)
        return f"[{phone}](tel:{clean})"

    lines = ["## 📇 Contacts\n"]

    for i, contact in enumerate(contacts, 1):
        # Contact header with edit/delete buttons
        lines.append(f"**{i}. {contact.name}** [✏️](cmd:contacts edit {contact.uid}) [🗑️](cmd:contacts delete {contact.uid})")

        if contact.emails:
            if len(contact.emails) == 1:
                lines.append(f"   📧 [{contact.emails[0]}](mailto:{contact.emails[0]})")
            else:
                # Multiple emails - show each on its own line
                for email in contact.emails:
                    lines.append(f"   📧 [{email}](mailto:{email})")

        if contact.phone:
            lines.append(f"   📞 {format_phone_link(contact.phone)}")

        if contact.organization:
            lines.append(f"   🏢 {contact.organization}")

        if contact.note:
            lines.append(f"   📝 {contact.note}")

        lines.append("")  # Empty line between contacts

    return "\n".join(lines)


@dataclass
class TodoItem:
    """Represents a CalDAV todo item (VTODO)."""
    uid: str
    summary: str
    description: Optional[str]
    due: Optional[datetime]
    priority: Optional[int]
    status: Optional[str]  # NEEDS-ACTION, IN-PROCESS, COMPLETED, CANCELLED
    calendar_name: str


def get_todos_from_calendar(
    url: str,
    username: str,
    password: str,
    calendar_name: str = "Calendar"
) -> List[TodoItem]:
    """Get all VTODO items from a CalDAV calendar."""
    todos = []

    try:
        client = create_caldav_client(url, username, password)

        # Try to get calendar from URL directly
        try:
            calendar = caldav.Calendar(client=client, url=url)
            calendars = [calendar]
        except Exception:
            principal = client.principal()
            calendars = principal.calendars()

        for cal in calendars:
            try:
                # Search for VTODO items
                vtodos = cal.todos(include_completed=False)
                for vtodo in vtodos:
                    try:
                        todo_obj = vtodo.vobject_instance.vtodo

                        # Get due date if present
                        due_dt = None
                        if hasattr(todo_obj, 'due'):
                            due_dt = to_naive_local(todo_obj.due.value)

                        # Get priority (1-9, lower is higher priority)
                        priority = None
                        if hasattr(todo_obj, 'priority'):
                            priority = int(todo_obj.priority.value)

                        # Get status
                        status = None
                        if hasattr(todo_obj, 'status'):
                            status = str(todo_obj.status.value)

                        todos.append(TodoItem(
                            uid=str(todo_obj.uid.value) if hasattr(todo_obj, 'uid') else "",
                            summary=str(todo_obj.summary.value) if hasattr(todo_obj, 'summary') else "No Title",
                            description=str(todo_obj.description.value) if hasattr(todo_obj, 'description') else None,
                            due=due_dt,
                            priority=priority,
                            status=status,
                            calendar_name=calendar_name
                        ))
                    except Exception as e:
                        logger.debug(f"Error parsing todo: {e}")
                        continue
            except Exception as e:
                logger.debug(f"Error fetching todos from calendar: {e}")
                continue

    except Exception as e:
        logger.error(f"Failed to get todos from {url}: {e}")

    return todos


def get_all_user_todos(user_id: int, db: Session = None) -> List[TodoItem]:
    """Get todos from all user's calendars with timeout protection."""
    calendars = get_user_calendars(user_id, db)
    all_todos = []

    for cal_config in calendars:
        url = cal_config.get('url', '')
        username = cal_config.get('username', '')
        password = cal_config.get('password', '')
        name = cal_config.get('name', 'Calendar')

        if url and username:
            try:
                # Run with timeout to prevent one slow calendar from blocking everything
                def fetch_todos():
                    return get_todos_from_calendar(url, username, password, name)

                todos = run_with_timeout(fetch_todos, timeout=CALDAV_OPERATION_TIMEOUT)
                if todos:
                    all_todos.extend(todos)
                else:
                    logger.warning(f"Skipping todos from calendar {name} - fetch timed out or failed")
            except Exception as e:
                logger.error(f"Error fetching todos from calendar {name}: {e}")
                # Continue to next calendar instead of failing completely

    # Sort by priority (lower number = higher priority), then by due date
    all_todos.sort(key=lambda t: (t.priority or 99, t.due or datetime.max))
    return all_todos


def add_todo_to_calendar(
    url: str,
    username: str,
    password: str,
    summary: str,
    description: str = "",
    due: Optional[datetime] = None,
    priority: int = 5,
    user_id: Optional[int] = None,
    db: Optional[Session] = None
) -> bool:
    """Add a VTODO item to a CalDAV calendar.
    
    Args:
        user_id: Optional user ID for built-in mode
        db: Optional database session for built-in mode
    """
    from icalendar import Calendar as ICalendar, Todo
    import uuid

    try:
        logger.info(f"Adding todo '{summary}' to calendar at {url}")
        
        # Check if using built-in server (direct file save)
        if password == "__USE_SESSION_AUTH__" and user_id and db:
            logger.info("Using built-in CalDAV storage (direct file save for todo)")
            
            # Create VTODO
            cal = ICalendar()
            cal.add('prodid', '-//Posterchanai//Calendar//EN')
            cal.add('version', '2.0')

            todo = Todo()
            todo.add('summary', summary)
            if description:
                todo.add('description', description)
            if due:
                todo.add('due', due)
            todo.add('priority', priority)
            todo.add('status', 'NEEDS-ACTION')
            todo.add('dtstamp', datetime.now())
            
            todo_uid = str(uuid.uuid4())
            todo.add('uid', todo_uid)

            cal.add_component(todo)
            ical_data = cal.to_ical().decode('utf-8')
            
            return _save_event_to_builtin(user_id, db, ical_data)

        # Otherwise use CalDAV protocol (for external servers)
        client = create_caldav_client(url, username, password)

        # Try to get calendar from URL directly
        try:
            calendar = caldav.Calendar(client=client, url=url)
        except Exception:
            principal = client.principal()
            calendars = principal.calendars()
            if not calendars:
                logger.error("No calendars found")
                return False
            calendar = calendars[0]

        # Create VTODO
        cal = ICalendar()
        cal.add('prodid', '-//Posterchanai//Todo//EN')
        cal.add('version', '2.0')

        todo = Todo()
        todo.add('summary', summary)
        todo.add('uid', str(uuid.uuid4()))
        todo.add('dtstamp', datetime.now())
        todo.add('created', datetime.now())
        todo.add('status', 'NEEDS-ACTION')
        todo.add('priority', priority)

        if description:
            todo.add('description', description)
        if due:
            todo.add('due', due)

        cal.add_component(todo)

        ical_data = cal.to_ical().decode('utf-8')
        calendar.save_event(ical_data)
        logger.info(f"Successfully added todo: {summary}")
        return True

    except Exception as e:
        logger.error(f"Failed to add todo: {e}", exc_info=True)
        return False


def delete_todo_from_calendar(
    url: str,
    username: str,
    password: str,
    todo_uid: str,
    user_id: Optional[int] = None,
    db: Optional[Session] = None
) -> bool:
    """Delete a VTODO item from a CalDAV calendar by UID.
    
    Args:
        user_id: Optional user ID for built-in mode
        db: Optional database session for built-in mode
    """
    try:
        # Check if using built-in server (uses storage proxy if configured)
        if password == "__USE_SESSION_AUTH__" and user_id and db:
            logger.info(f"Using built-in CalDAV storage (delete via proxy) for todo {todo_uid}")
            
            from app.models import User
            from app.services.dav_storage_proxy import DAVStorageProxy
            
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            
            # Use storage proxy (will fallback to local if not configured)
            proxy = DAVStorageProxy(db, user.username, 'caldav')
            
            filepath = f"{todo_uid}.ics"
            success = proxy.delete_file(filepath)
            
            if success:
                logger.info(f"Deleted todo {todo_uid} from built-in storage")
                return True
            else:
                logger.warning(f"Todo file not found or failed to delete: {filepath}")
                return False
        
        # Otherwise use CalDAV protocol (for external servers)
        client = create_caldav_client(url, username, password)

        # Try to get calendar from URL directly
        try:
            calendar = caldav.Calendar(client=client, url=url)
            calendars = [calendar]
        except Exception:
            principal = client.principal()
            calendars = principal.calendars()

        for cal in calendars:
            try:
                vtodos = cal.todos(include_completed=False)
                for vtodo in vtodos:
                    try:
                        if hasattr(vtodo.vobject_instance.vtodo, 'uid'):
                            if str(vtodo.vobject_instance.vtodo.uid.value) == todo_uid:
                                vtodo.delete()
                                logger.info(f"Deleted todo with UID: {todo_uid}")
                                return True
                    except Exception as e:
                        logger.debug(f"Error checking todo: {e}")
                        continue
            except Exception as e:
                logger.debug(f"Error searching calendar: {e}")
                continue

        logger.warning(f"Todo with UID {todo_uid} not found")
        return False

    except Exception as e:
        logger.error(f"Failed to delete todo: {e}")
        return False


def delete_event_from_calendar(
    url: str,
    username: str,
    password: str,
    event_uid: str,
    user_id: Optional[int] = None,
    db: Optional[Session] = None
) -> bool:
    """Delete a VEVENT item from a CalDAV calendar by UID.
    
    Args:
        user_id: Optional user ID for built-in mode
        db: Optional database session for built-in mode
    """
    try:
        # Check if using built-in server (uses storage proxy if configured)
        if password == "__USE_SESSION_AUTH__" and user_id and db:
            logger.info(f"Using built-in CalDAV storage (delete via proxy) for event {event_uid}")
            
            from app.models import User
            from app.services.dav_storage_proxy import DAVStorageProxy
            
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            
            # Use storage proxy (will fallback to local if not configured)
            proxy = DAVStorageProxy(db, user.username, 'caldav')
            
            filepath = f"{event_uid}.ics"
            success = proxy.delete_file(filepath)
            
            if success:
                logger.info(f"Deleted event {event_uid} from built-in storage")
                return True
            else:
                logger.warning(f"Event file not found or failed to delete: {filepath}")
                return False
        
        # Otherwise use CalDAV protocol (for external servers)
        client = create_caldav_client(url, username, password)

        # Try to get calendar from URL directly
        try:
            calendar = caldav.Calendar(client=client, url=url)
            calendars = [calendar]
        except Exception:
            principal = client.principal()
            calendars = principal.calendars()

        for cal in calendars:
            try:
                # Try to get event directly by UID (much faster than fetching all)
                try:
                    event = cal.event_by_uid(event_uid)
                    if event:
                        event.delete()
                        logger.info(f"Deleted event with UID: {event_uid}")
                        return True
                except Exception:
                    pass

                # Fallback: search by UID
                try:
                    events = cal.search(uid=event_uid)
                    if events:
                        events[0].delete()
                        logger.info(f"Deleted event with UID: {event_uid}")
                        return True
                except Exception:
                    pass

            except Exception as e:
                logger.debug(f"Error searching calendar: {e}")
                continue

        logger.warning(f"Event with UID {event_uid} not found")
        return False

    except Exception as e:
        logger.error(f"Failed to delete event: {e}")
        return False


def format_todos_for_display(todos: List[TodoItem]) -> str:
    """Format todos for display with action buttons."""
    if not todos:
        return "No todos found. Add one with `todo add <task>`"

    lines = []
    for idx, todo in enumerate(todos, 1):
        # Priority indicator
        if todo.priority and todo.priority <= 3:
            priority_icon = "🔴"  # High priority
        elif todo.priority and todo.priority <= 6:
            priority_icon = "🟡"  # Medium priority
        else:
            priority_icon = "🟢"  # Low priority

        # Due date
        due_str = ""
        if todo.due:
            due_str = f" (due {todo.due.strftime('%b %d')})"

        # Delete button
        delete_btn = f"[Done](cmd:todo rm {idx})"

        lines.append(f"**{idx}.** {priority_icon} {todo.summary}{due_str} {delete_btn}")

    return "\n".join(lines)
