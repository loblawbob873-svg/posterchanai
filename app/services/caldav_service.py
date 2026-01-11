"""
CalDAV/CardDAV Service - Calendar and Contacts integration.

Provides:
- CalDAV calendar access (list events, add events)
- CardDAV contact access (search contacts)
- Daily schedule summaries
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

import caldav
import vobject
from icalendar import Calendar, Event
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import User, UserSetting

logger = logging.getLogger(__name__)


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


@dataclass
class Contact:
    """Represents a contact."""
    uid: str
    name: str
    email: Optional[str]
    phone: Optional[str]
    organization: Optional[str]
    note: Optional[str]


def get_user_calendars(user_id: int, db: Session = None) -> List[Dict[str, str]]:
    """Get user's configured CalDAV calendars."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
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
            except json.JSONDecodeError:
                logger.error(f"Invalid caldav_calendars JSON for user {user_id}")

        return calendars
    finally:
        if close_db:
            db.close()


def get_user_contacts_config(user_id: int, db: Session = None) -> Optional[Dict[str, str]]:
    """Get user's CardDAV contacts configuration."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        setting = db.query(UserSetting).filter(
            UserSetting.user_id == user_id,
            UserSetting.key == "carddav_config"
        ).first()

        if setting and setting.value:
            import json
            try:
                return json.loads(setting.value)
            except json.JSONDecodeError:
                logger.error(f"Invalid carddav_config JSON for user {user_id}")

        return None
    finally:
        if close_db:
            db.close()


def connect_calendar(url: str, username: str, password: str) -> Optional[caldav.DAVClient]:
    """Connect to a CalDAV server."""
    try:
        client = caldav.DAVClient(url=url, username=username, password=password)
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
        return dt.astimezone().replace(tzinfo=None)
    return dt


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

    # Ensure start/end dates are naive for comparison
    start_date = to_naive_local(start_date)
    end_date = to_naive_local(end_date)

    try:
        client = caldav.DAVClient(url=url, username=username, password=password)
        principal = client.principal()

        # Try to get calendars from the URL directly first
        try:
            calendar = caldav.Calendar(client=client, url=url)
            calendars = [calendar]
        except Exception:
            calendars = principal.calendars()

        for cal in calendars:
            try:
                cal_events = cal.date_search(start=start_date, end=end_date, expand=True)
                for event in cal_events:
                    try:
                        vevent = event.vobject_instance.vevent

                        # Get start time (convert to naive local)
                        start = vevent.dtstart.value
                        start_dt = to_naive_local(start)

                        # Get end time (convert to naive local)
                        end_dt = None
                        if hasattr(vevent, 'dtend'):
                            end_dt = to_naive_local(vevent.dtend.value)

                        events.append(CalendarEvent(
                            uid=str(vevent.uid.value) if hasattr(vevent, 'uid') else "",
                            summary=str(vevent.summary.value) if hasattr(vevent, 'summary') else "No Title",
                            description=str(vevent.description.value) if hasattr(vevent, 'description') else None,
                            start=start_dt,
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

    return events


def get_all_user_events(
    user_id: int,
    start_date: datetime,
    end_date: datetime,
    db: Session = None
) -> List[CalendarEvent]:
    """Get events from all user's calendars for a date range."""
    calendars = get_user_calendars(user_id, db)
    all_events = []

    for cal_config in calendars:
        url = cal_config.get('url', '')
        username = cal_config.get('username', '')
        password = cal_config.get('password', '')
        name = cal_config.get('name', 'Calendar')

        if url and username:
            events = get_events_for_date_range(url, username, password, start_date, end_date, name)
            all_events.extend(events)

    # Sort by start time
    all_events.sort(key=lambda e: e.start)
    return all_events


def add_event_to_calendar(
    url: str,
    username: str,
    password: str,
    summary: str,
    description: str,
    start_time: datetime,
    end_time: Optional[datetime] = None,
    location: Optional[str] = None
) -> bool:
    """Add an event to a CalDAV calendar."""
    try:
        logger.info(f"Adding event '{summary}' to calendar at {url}")
        logger.debug(f"Event times: start={start_time}, end={end_time}")

        client = caldav.DAVClient(url=url, username=username, password=password)

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

        cal = Calendar()
        cal.add('prodid', '-//Posterchanai//Calendar//EN')
        cal.add('version', '2.0')

        event = Event()
        event.add('summary', summary)
        event.add('dtstart', start_time)
        event.add('dtend', end_time)
        if description:
            event.add('description', description)
        if location:
            event.add('location', location)
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
                               headers=headers, data=propfind_body, timeout=30)

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

                vcf_resp = requests.get(vcf_url, auth=HTTPBasicAuth(username, password), timeout=10)
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

                email = None
                if hasattr(vcard, 'email'):
                    email = str(vcard.email.value)

                phone = None
                if hasattr(vcard, 'tel'):
                    phone = str(vcard.tel.value)

                org = None
                if hasattr(vcard, 'org'):
                    org = str(vcard.org.value[0]) if vcard.org.value else None

                note = None
                if hasattr(vcard, 'note'):
                    note = str(vcard.note.value)

                # Check if query matches (empty query matches all)
                searchable = f"{name} {email or ''} {phone or ''} {org or ''} {note or ''}".lower()
                if not query_lower or query_lower in searchable:
                    contacts.append(Contact(
                        uid=str(vcard.uid.value) if hasattr(vcard, 'uid') else "",
                        name=name,
                        email=email,
                        phone=phone,
                        organization=org,
                        note=note
                    ))

            except Exception as e:
                logger.debug(f"Error parsing vCard {vcf_path}: {e}")
                continue

    except Exception as e:
        logger.error(f"Failed to search contacts: {e}")

    return contacts


def get_user_contacts(user_id: int, query: str, db: Session = None) -> List[Contact]:
    """Search contacts using user's CardDAV configuration."""
    config = get_user_contacts_config(user_id, db)
    if not config:
        return []

    url = config.get('url', '')
    username = config.get('username', '')
    password = config.get('password', '')

    if not url or not username:
        return []

    return search_contacts(url, username, password, query)


def format_events_for_display(events: List[CalendarEvent], include_description: bool = False) -> str:
    """Format events for display."""
    if not events:
        return "No events found."

    lines = []
    current_date = None

    for event in events:
        event_date = event.start.date()
        if event_date != current_date:
            current_date = event_date
            lines.append(f"\n**{event_date.strftime('%A, %B %d, %Y')}**")

        time_str = event.start.strftime("%I:%M %p")
        if event.end:
            time_str += f" - {event.end.strftime('%I:%M %p')}"

        line = f"- {time_str}: {event.summary}"
        if event.location:
            line += f" @ {event.location}"
        lines.append(line)

        if include_description and event.description:
            lines.append(f"  _{event.description}_")

    return "\n".join(lines)


def format_contacts_for_display(contacts: List[Contact]) -> str:
    """Format contacts for display."""
    if not contacts:
        return "No contacts found."

    lines = []
    for contact in contacts:
        lines.append(f"\n**{contact.name}**")
        if contact.email:
            lines.append(f"  Email: {contact.email}")
        if contact.phone:
            lines.append(f"  Phone: {contact.phone}")
        if contact.organization:
            lines.append(f"  Organization: {contact.organization}")
        if contact.note:
            lines.append(f"  Note: {contact.note}")

    return "\n".join(lines)
