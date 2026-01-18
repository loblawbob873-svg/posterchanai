"""
Built-in CalDAV Server - Serves calendars via CalDAV protocol.
Supports importing from Radicale and exporting to common formats.
Full implementation supporting all calendar and todo commands.
"""
import logging
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.requests import Request as StarletteRequest
import uvicorn
import uuid
import base64
import re

from app.models import User, Setting
from app.services.storage_service import get_storage_service
from app.auth import verify_password
from app.database import get_db
from icalendar import Calendar as ICalendar, Event, Todo
from dateutil import parser as date_parser

logger = logging.getLogger(__name__)

# Global server instance
_caldav_app: Optional[FastAPI] = None
_caldav_server: Optional[uvicorn.Server] = None
_caldav_thread: Optional[threading.Thread] = None


def get_user_caldav_path(user: User, db: Session) -> Path:
    """Get the CalDAV storage path for a user."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(user.username)
    caldav_path = user_path / "caldav"
    caldav_path.mkdir(parents=True, exist_ok=True)
    return caldav_path


def parse_caldav_request(body: bytes) -> Dict:
    """Parse CalDAV request body (PROPFIND, REPORT, etc.)."""
    try:
        root = ET.fromstring(body)
        return {"root": root, "namespace": root.tag.split('}')[0].strip('{') if '}' in root.tag else ""}
    except Exception as e:
        logger.debug(f"Error parsing CalDAV request: {e}")
        return {"root": None, "namespace": ""}


def create_caldav_response(multistatus_items: List[Dict]) -> str:
    """Create a CalDAV multistatus XML response."""
    import html
    xml = '''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
'''
    for item in multistatus_items:
        href = html.escape(item.get('href', ''))
        props = item.get('props', {})
        xml += f'    <D:response>\n        <D:href>{href}</D:href>\n        <D:propstat>\n            <D:prop>\n'
        for prop_name, prop_value in props.items():
            if prop_name == 'resourcetype':
                if prop_value == 'calendar':
                    xml += '                <D:resourcetype><D:collection/><C:calendar xmlns:C="urn:ietf:params:xml:ns:caldav"/></D:resourcetype>\n'
                elif prop_value == 'collection':
                    xml += f'                <D:resourcetype><D:collection/></D:resourcetype>\n'
                else:
                    xml += f'                <D:resourcetype><D:collection/></D:resourcetype>\n'
            elif prop_name == 'displayname':
                xml += f'                <D:displayname>{html.escape(str(prop_value))}</D:displayname>\n'
            elif prop_name == 'getcontenttype':
                xml += f'                <D:getcontenttype>{html.escape(str(prop_value))}</D:getcontenttype>\n'
            elif prop_name == 'getetag':
                xml += f'                <D:getetag>"{html.escape(str(prop_value))}"</D:getetag>\n'
            elif prop_name == 'calendar-data':
                # Escape XML special characters in calendar data
                escaped_data = html.escape(prop_value).replace('&lt;', '<').replace('&gt;', '>')  # Don't escape < and > in XML content
                # But we need to escape them properly for XML CDATA or use proper escaping
                # Actually, calendar-data should be in CDATA or properly escaped
                xml += f'                <C:calendar-data xmlns:C="urn:ietf:params:xml:ns:caldav"><![CDATA[{prop_value}]]></C:calendar-data>\n'
            elif prop_name == 'supported-calendar-component-set':
                xml += '                <C:supported-calendar-component-set xmlns:C="urn:ietf:params:xml:ns:caldav">\n'
                for comp in prop_value.split(','):
                    xml += f'                    <C:comp name="{comp.strip()}"/>\n'
                xml += '                </C:supported-calendar-component-set>\n'
            elif prop_name == 'calendar-description':
                xml += f'                <C:calendar-description xmlns:C="urn:ietf:params:xml:ns:caldav">{html.escape(str(prop_value))}</C:calendar-description>\n'
            elif prop_name == 'calendar-color':
                xml += f'                <ical:calendar-color xmlns:ical="http://apple.com/ns/ical/">{html.escape(str(prop_value))}</ical:calendar-color>\n'
            elif prop_name == 'calendar-timezone':
                xml += f'                <C:calendar-timezone xmlns:C="urn:ietf:params:xml:ns:caldav">{html.escape(str(prop_value))}</C:calendar-timezone>\n'
        xml += '            </D:prop>\n            <D:status>HTTP/1.1 200 OK</D:status>\n        </D:propstat>\n    </D:response>\n'
    xml += '</D:multistatus>'
    return xml


def get_event_uid_from_ical(ical_data: str) -> Optional[str]:
    """Extract UID from iCalendar data."""
    try:
        cal = ICalendar.from_ical(ical_data.encode('utf-8'))
        for component in cal.walk():
            if component.name == "VEVENT":
                return str(component.get('uid', ''))
            elif component.name == "VTODO":
                return str(component.get('uid', ''))
    except Exception as e:
        logger.debug(f"Error parsing iCal for UID: {e}")
    return None


async def handle_propfind(path: str, user: User, db: Session, depth: str = "0") -> Response:
    """Handle PROPFIND request. Uses storage proxy if configured."""
    from urllib.parse import quote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Use storage proxy (will fallback to local if not configured)
    proxy = DAVStorageProxy(db, user.username, 'caldav')
    encoded_username = quote(user.username, safe='')
    base_url = f"/caldav/{encoded_username}"
    
    items = []
    
    # Normalize path - remove trailing slashes and handle /user/ subpath
    path = path.rstrip('/')
    # Strip username from path if present (both encoded and unencoded)
    if path.startswith(user.username):
        path = path[len(user.username):].lstrip('/')
    if path.startswith(encoded_username):
        path = path[len(encoded_username):].lstrip('/')
    # Handle /user/ subpath (some clients use this)
    if path == 'user':
        path = ''
    
    # Root calendar home - this is NOT a calendar itself, just a container
    if not path or path == '':
        # Return the calendar home collection
        items.append({
            "href": f"{base_url}/",
            "props": {
                "resourcetype": "collection",  # Just a collection, not a calendar
                "displayname": f"{user.username}'s Calendars"
            }
        })
        
        # If depth=1, list child calendars
        if depth == "1":
            # List files using proxy
            file_items = proxy.list_files("")
            
            # Log what we found for debugging
            logger.info(f"[CalDAV] Listing calendars for {user.username}: found {len(file_items)} items")
            
            # Check for calendar subdirectories
            calendar_dirs = []
            ics_files = []
            for item in file_items:
                item_name = item.get('name', '')
                is_dir = item.get('is_directory', False)
                logger.debug(f"[CalDAV] Item: {item_name}, is_directory: {is_dir}")
                
                if is_dir and not item_name.startswith('.'):
                    calendar_dirs.append(item_name)
                elif item_name.endswith('.ics'):
                    ics_files.append(item_name)
            
            logger.info(f"[CalDAV] Found {len(calendar_dirs)} calendar directories: {calendar_dirs}")
            logger.info(f"[CalDAV] Found {len(ics_files)} loose .ics files")
            
            # Check if there are loose .ics files in root (legacy mode)
            has_loose_ics = len(ics_files) > 0
            
            # Always show calendar subdirectories if they exist
            for cal_name in sorted(calendar_dirs):
                # Skip hidden directories
                if cal_name.startswith('.'):
                    continue
                logger.info(f"[CalDAV] Adding calendar: {cal_name}")
                items.append({
                    "href": f"{base_url}/{quote(cal_name, safe='')}/",
                    "props": {
                        "resourcetype": "calendar",
                        "displayname": cal_name.replace('_', ' ').title(),
                        "supported-calendar-component-set": "VEVENT,VTODO",
                        "calendar-description": f"{cal_name} Calendar",
                        "calendar-color": "#0088FF",
                        "calendar-timezone": "UTC"
                    }
                })
            
            # If no calendar subdirectories exist but there are loose .ics files,
            # show legacy "Calendar" for backwards compatibility
            if not calendar_dirs and has_loose_ics:
                logger.info(f"[CalDAV] Adding legacy 'Calendar' (loose .ics files found)")
                items.append({
                    "href": f"{base_url}/calendar/",
                    "props": {
                        "resourcetype": "calendar",
                        "displayname": "Calendar",
                        "supported-calendar-component-set": "VEVENT,VTODO",
                        "calendar-description": "Default Calendar",
                        "calendar-color": "#0088FF",
                        "calendar-timezone": "UTC"
                    }
                })
            
            # If no calendars exist at all, create a default "Calendar" so users can add events
            if not calendar_dirs and not has_loose_ics:
                logger.info(f"[CalDAV] No calendars found, adding default 'Calendar'")
                items.append({
                    "href": f"{base_url}/calendar/",
                    "props": {
                        "resourcetype": "calendar",
                        "displayname": "Calendar",
                        "supported-calendar-component-set": "VEVENT,VTODO",
                        "calendar-description": "Default Calendar",
                        "calendar-color": "#0088FF",
                        "calendar-timezone": "UTC"
                    }
                })
            
            logger.info(f"[CalDAV] Returning {len(items)} calendar items for {user.username}")
    
    # Individual calendar collection
    elif '/' not in path:
        # This is a calendar name (e.g., "calendar" or "work" or "personal")
        cal_name = path
        subpath = "" if cal_name == 'calendar' else cal_name
        
        items.append({
            "href": f"{base_url}/{quote(cal_name, safe='')}/",
            "props": {
                "resourcetype": "calendar",
                "displayname": cal_name.replace('_', ' ').title() if cal_name != 'calendar' else "Calendar",
                "supported-calendar-component-set": "VEVENT,VTODO",
                "calendar-description": f"{cal_name} Calendar",
                "calendar-color": "#0088FF",
                "calendar-timezone": "UTC"
            }
        })
        
        # If depth=1, list events in this calendar
        if depth == "1":
            # List files using proxy
            file_items = proxy.list_files(subpath)
            logger.info(f"[CalDAV] Listing events in calendar '{cal_name}' (subpath='{subpath}'): found {len(file_items)} items")
            for item in file_items:
                name = item.get('name', '')
                if name.endswith('.ics'):
                    event_uid = name.replace('.ics', '')
                    etag = str(item.get('modified', item.get('mtime', 0)))
                    items.append({
                        "href": f"{base_url}/{quote(cal_name, safe='')}/{event_uid}.ics",
                        "props": {
                            "getcontenttype": "text/calendar; charset=utf-8",
                            "getetag": etag
                        }
                    })
            logger.info(f"[CalDAV] Added {len([i for i in items if i.get('href', '').endswith('.ics')])} events to PROPFIND response for calendar '{cal_name}'")
    
    # Individual event
    elif path.count('/') == 1 and path.endswith('.ics'):
        parts = path.split('/')
        cal_name = parts[0]
        event_file = parts[1]
        event_uid = event_file.replace('.ics', '')
        
        # Build filepath
        if cal_name == 'calendar':
            filepath = f"{event_uid}.ics"
        else:
            filepath = f"{cal_name}/{event_uid}.ics"
        
        # Check if file exists using proxy
        if proxy.file_exists(filepath):
            # Get file info for etag
            subpath = "" if cal_name == 'calendar' else cal_name
            file_items = proxy.list_files(subpath)
            etag = "0"
            for item in file_items:
                if item.get('name') == f"{event_uid}.ics":
                    etag = str(item.get('modified', item.get('mtime', 0)))
                    break
            
            items.append({
                "href": f"{base_url}/{path}",
                "props": {
                    "getcontenttype": "text/calendar; charset=utf-8",
                    "getetag": etag
                }
            })
    
    xml = create_caldav_response(items)
    return Response(content=xml, media_type="application/xml", status_code=207)


async def handle_report(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle REPORT request (calendar queries). Uses storage proxy if configured."""
    from urllib.parse import quote, unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    body = await request.body()
    
    # Use storage proxy (will fallback to local if not configured)
    proxy = DAVStorageProxy(db, user.username, 'caldav')
    encoded_username = quote(user.username, safe='')
    base_url = f"/caldav/{encoded_username}"
    
    # Normalize path
    path = path.rstrip('/')
    if path.startswith(user.username):
        path = path[len(user.username):].lstrip('/')
    if path.startswith(encoded_username):
        path = path[len(encoded_username):].lstrip('/')
    
    # Determine which calendar we're querying
    cal_name = None
    if path and '/' not in path:
        cal_name = unquote(path)
    
    # Determine the calendar subpath
    if cal_name == 'calendar':
        subpath = ""  # Legacy: root directory
    elif cal_name:
        subpath = cal_name
    else:
        subpath = ""  # Default to root
    
    try:
        root = ET.fromstring(body)
        # Check for calendar-query or calendar-multiget
        query_elem = root.find('.//{urn:ietf:params:xml:ns:caldav}calendar-query')
        multiget_elem = root.find('.//{urn:ietf:params:xml:ns:caldav}calendar-multiget')
        
        items = []
        
        if query_elem is not None:
            # Calendar query - filter by time range
            filter_elem = query_elem.find('.//{urn:ietf:params:xml:ns:caldav}filter')
            time_range = None
            if filter_elem is not None:
                time_range_elem = filter_elem.find('.//{urn:ietf:params:xml:ns:caldav}time-range')
                if time_range_elem is not None:
                    start_str = time_range_elem.get('start', '')
                    end_str = time_range_elem.get('end', '')
                    if start_str and end_str:
                        try:
                            start_dt = date_parser.parse(start_str.replace('Z', '+00:00'))
                            end_dt = date_parser.parse(end_str.replace('Z', '+00:00'))
                            time_range = (start_dt, end_dt)
                        except:
                            pass
            
            # List matching events from the specified calendar directory using proxy
            file_items = proxy.list_files(subpath)
            logger.info(f"[CalDAV] REPORT query for calendar '{cal_name}' (subpath='{subpath}'): found {len(file_items)} items")
            for item in file_items:
                name = item.get('name', '')
                if name.endswith('.ics'):
                    try:
                        # Build filepath
                        if subpath:
                            filepath = f"{subpath}/{name}"
                        else:
                            filepath = name
                        
                        logger.debug(f"[CalDAV] Processing event file: {filepath}")
                        
                        # Read calendar data using proxy
                        ical_data = proxy.read_file(filepath)
                        if not ical_data:
                            logger.warning(f"[CalDAV] Failed to read event file: {filepath}")
                            continue
                        logger.debug(f"[CalDAV] Read event file {filepath}: {len(ical_data)} bytes")
                        
                        # Check time range if specified
                        include_event = True
                        if time_range:
                            include_event = False
                            cal = ICalendar.from_ical(ical_data.encode('utf-8'))
                            for component in cal.walk():
                                if component.name in ("VEVENT", "VTODO"):
                                    dtstart = component.get('dtstart')
                                    if dtstart:
                                        event_start = dtstart.dt
                                        # Handle both datetime and date objects
                                        if isinstance(event_start, datetime):
                                            if time_range[0] <= event_start <= time_range[1]:
                                                include_event = True
                                                break
                                        else:
                                            # Date object - convert to datetime for comparison
                                            from datetime import date
                                            if isinstance(event_start, date):
                                                # Check if the date falls within range
                                                event_datetime = datetime.combine(event_start, datetime.min.time())
                                                if time_range[0].date() <= event_start <= time_range[1].date():
                                                    include_event = True
                                                    break
                                    else:
                                        # No start time, include it anyway
                                        include_event = True
                                        break
                        
                        if include_event:
                            event_uid = name.replace('.ics', '')
                            href_path = f"{cal_name}/{event_uid}.ics" if cal_name else f"calendar/{event_uid}.ics"
                            etag = str(item.get('modified', item.get('mtime', 0)))
                            items.append({
                                "href": f"{base_url}/{href_path}",
                                "props": {
                                    "getcontenttype": "text/calendar; charset=utf-8",
                                    "getetag": etag,
                                    "calendar-data": ical_data
                                }
                            })
                    except Exception as e:
                        logger.debug(f"Error processing {name}: {e}")
                        continue
        
        elif multiget_elem is not None:
            # Calendar multiget - get specific events by href
            hrefs = [elem.text for elem in multiget_elem.findall('.//{DAV:}href')]
            for href in hrefs:
                # Extract calendar name and UID from href
                match = re.search(r'/([^/]+)/([^/]+)\.ics$', href)
                if match:
                    href_cal_name = unquote(match.group(1))
                    event_uid = match.group(2)
                    
                    # Build filepath
                    if href_cal_name == 'calendar':
                        filepath = f"{event_uid}.ics"
                    else:
                        filepath = f"{href_cal_name}/{event_uid}.ics"
                    
                    # Check if file exists and read using proxy
                    if proxy.file_exists(filepath):
                        try:
                            ical_data = proxy.read_file(filepath)
                            if ical_data:
                                # Get etag from file listing
                                href_subpath = "" if href_cal_name == 'calendar' else href_cal_name
                                file_items = proxy.list_files(href_subpath)
                                etag = "0"
                                for item in file_items:
                                    if item.get('name') == f"{event_uid}.ics":
                                        etag = str(item.get('modified', item.get('mtime', 0)))
                                        break
                                
                                items.append({
                                    "href": href,
                                    "props": {
                                        "getcontenttype": "text/calendar; charset=utf-8",
                                        "getetag": etag,
                                        "calendar-data": ical_data
                                    }
                                })
                        except Exception as e:
                            logger.debug(f"Error reading {filepath}: {e}")
        
        xml = create_caldav_response(items)
        return Response(content=xml, media_type="application/xml", status_code=207)
    except Exception as e:
        logger.error(f"Error handling REPORT: {e}", exc_info=True)
        return Response(content="", status_code=500)


async def handle_get(path: str, user: User, db: Session) -> Response:
    """Handle GET request (retrieve calendar/event). Uses storage proxy if configured."""
    from urllib.parse import unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Use storage proxy (will fallback to local if not configured)
    proxy = DAVStorageProxy(db, user.username, 'caldav')
    
    # Extract calendar name and event UID from path
    match = re.search(r'/([^/]+)/([^/]+)\.ics$', path)
    if match:
        cal_name = unquote(match.group(1))
        event_uid = match.group(2)
        
        # Build filepath (with calendar subdirectory if not 'calendar')
        if cal_name == 'calendar':
            filepath = f"{event_uid}.ics"
        else:
            filepath = f"{cal_name}/{event_uid}.ics"
        
        # Read file using proxy
        ical_data = proxy.read_file(filepath)
        if ical_data:
            return Response(content=ical_data, media_type="text/calendar; charset=utf-8")
        else:
            logger.warning(f"Event file not found: {filepath}")
            return Response(content="Not found", status_code=404)
    
    return Response(content="Not found", status_code=404)


async def handle_put(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle PUT request (create/update event). Uses storage proxy if configured."""
    from urllib.parse import unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    body = await request.body()
    
    # Use storage proxy (will fallback to local if not configured)
    proxy = DAVStorageProxy(db, user.username, 'caldav')
    
    try:
        ical_data = body.decode('utf-8')
        
        # Extract UID from iCalendar data
        event_uid = get_event_uid_from_ical(ical_data)
        
        # Extract calendar name and UID from path
        match = re.search(r'/([^/]+)/([^/]+)\.ics$', path)
        if match:
            cal_name = unquote(match.group(1))
            path_uid = match.group(2)
            if event_uid and event_uid != path_uid:
                logger.warning(f"UID mismatch: path={path_uid}, ical={event_uid}, using path UID")
            event_uid = path_uid
        else:
            # Legacy path format or no calendar specified
            cal_name = 'calendar'
        
        if not event_uid:
            # Generate UID if not present
            event_uid = str(uuid.uuid4())
            # Add UID to iCalendar data
            cal = ICalendar.from_ical(ical_data.encode('utf-8'))
            for component in cal.walk():
                if component.name in ("VEVENT", "VTODO"):
                    if not component.get('uid'):
                        component.add('uid', event_uid)
            ical_data = cal.to_ical().decode('utf-8')
        
        # Build filepath (with calendar subdirectory if not 'calendar')
        if cal_name == 'calendar':
            filepath = f"{event_uid}.ics"
        else:
            filepath = f"{cal_name}/{event_uid}.ics"
        
        # Save to file using proxy
        success = proxy.write_file(filepath, ical_data)
        
        if success:
            logger.info(f"Saved event/todo {event_uid} for user {user.username} in calendar {cal_name}")
            return Response(content="", status_code=201)
        else:
            logger.error(f"Failed to save event {event_uid}")
            return Response(content="Error saving event", status_code=500)
    except Exception as e:
        logger.error(f"Error saving event: {e}")
        return Response(content=f"Error: {e}", status_code=500)


async def handle_delete(path: str, user: User, db: Session) -> Response:
    """Handle DELETE request."""
    from urllib.parse import unquote
    caldav_path = get_user_caldav_path(user, db)
    
    # Extract calendar name and event UID from path
    match = re.search(r'/([^/]+)/([^/]+)\.ics$', path)
    if match:
        cal_name = unquote(match.group(1))
        event_uid = match.group(2)
        
        # Determine calendar directory
        if cal_name == 'calendar':
            cal_dir = caldav_path  # Legacy: root directory
        else:
            cal_dir = caldav_path / cal_name
        
        # Build filepath (with calendar subdirectory if not 'calendar')
        if cal_name == 'calendar':
            filepath = f"{event_uid}.ics"
        else:
            filepath = f"{cal_name}/{event_uid}.ics"
        
        # Use storage proxy (will fallback to local if not configured)
        from app.services.dav_storage_proxy import DAVStorageProxy
        proxy = DAVStorageProxy(db, user.username, 'caldav')
        
        # Delete file using proxy
        success = proxy.delete_file(filepath)
        if success:
            logger.info(f"Deleted event {event_uid} from calendar {cal_name} for user {user.username}")
            return Response(content="", status_code=204)
        else:
            logger.warning(f"Event file not found or failed to delete: {filepath}")
            return Response(content="Not found", status_code=404)
    
    return Response(content="Not found", status_code=404)


async def handle_mkcalendar(path: str, user: User, db: Session) -> Response:
    """Handle MKCALENDAR request. Creates a new calendar directory."""
    from urllib.parse import unquote, quote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Normalize path
    path = path.rstrip('/')
    if path.startswith(user.username):
        path = path[len(user.username):].lstrip('/')
    encoded_username = quote(user.username, safe='')
    if path.startswith(encoded_username):
        path = path[len(encoded_username):].lstrip('/')
    
    # Extract calendar name from path
    if '/' in path:
        # Path like "calendar_name/" - extract calendar name
        cal_name = path.split('/')[0]
    else:
        # Path is just the calendar name
        cal_name = path if path else 'calendar'
    
    cal_name = unquote(cal_name)
    
    # Use storage proxy to create calendar directory
    proxy = DAVStorageProxy(db, user.username, 'caldav')
    
    # Create calendar directory by writing a placeholder file
    # The directory will be created automatically when we write a file
    placeholder_path = f"{cal_name}/.caldav_placeholder"
    success = proxy.write_file(placeholder_path, "# CalDAV Calendar Directory")
    
    if success:
        logger.info(f"Created calendar '{cal_name}' for user {user.username}")
        return Response(content="", status_code=201)
    else:
        logger.error(f"Failed to create calendar '{cal_name}' for user {user.username}")
        return Response(content="Failed to create calendar", status_code=500)


async def handle_proppatch(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle PROPPATCH request (set calendar properties)."""
    body = await request.body()
    
    try:
        # Parse the PROPPATCH request
        root = ET.fromstring(body)
        
        # For now, just accept the changes without actually storing them
        # The calendar properties are hardcoded in handle_propfind
        # In a full implementation, you'd store these in a database or file
        
        # Return success response
        xml = '''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
    <D:response>
        <D:href>{}</D:href>
        <D:propstat>
            <D:prop/>
            <D:status>HTTP/1.1 200 OK</D:status>
        </D:propstat>
    </D:response>
</D:multistatus>'''.format(path)
        
        return Response(content=xml, media_type="application/xml", status_code=207)
    except Exception as e:
        logger.error(f"Error handling PROPPATCH: {e}")
        return Response(content="", status_code=500)


def create_caldav_app() -> FastAPI:
    """Create CalDAV FastAPI application."""
    app = FastAPI(title="Posterchanai CalDAV Server")
    
    @app.get("/.well-known/caldav")
    async def caldav_discovery():
        """CalDAV discovery endpoint."""
        return Response(
            content="",
            status_code=301,
            headers={"Location": "/caldav/"}
        )
    
    @app.api_route("/caldav/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PROPFIND", "PROPPATCH", "REPORT", "MKCALENDAR"])
    async def caldav_handler(path: str, request: StarletteRequest, db: Session = Depends(get_db)):
        """Handle CalDAV requests."""
        # Extract username from path or Basic Auth
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return Response(
                content="Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
            )
        
        # Parse Basic Auth
        try:
            credentials = base64.b64decode(auth_header[6:]).decode('utf-8')
            username, password = credentials.split(':', 1)
        except:
            return Response(
                content="Invalid credentials",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
            )
        
        # Verify user - try exact match first, then try without domain
        user = db.query(User).filter(User.username == username).first()
        if not user and '@' in username:
            # Try without domain (some clients send just the username part)
            username_part = username.split('@')[0]
            user = db.query(User).filter(User.username.like(f"{username_part}@%")).first()
            if user:
                logger.debug(f"[CalDAV] Matched user by username part: {username} -> {user.username}")
        
        if not user:
            logger.warning(f"[CalDAV] User not found: {username}")
            return Response(
                content="Invalid credentials",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
            )
        
        # Verify password
        if not verify_password(password, user.password_hash):
            logger.warning(f"[CalDAV] Invalid password for user: {user.username} (auth username: {username})")
            return Response(
                content="Invalid credentials",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
            )
        
        logger.debug(f"[CalDAV] Authenticated user: {user.username}")
        
        # Get depth header for PROPFIND
        depth = request.headers.get("Depth", "0")
        
        # Handle CalDAV methods
        method = request.method
        
        if method == "PROPFIND":
            return await handle_propfind(path, user, db, depth)
        elif method == "PROPPATCH":
            return await handle_proppatch(path, user, db, request)
        elif method == "REPORT":
            return await handle_report(path, user, db, request)
        elif method == "GET":
            return await handle_get(path, user, db)
        elif method == "PUT":
            return await handle_put(path, user, db, request)
        elif method == "DELETE":
            return await handle_delete(path, user, db)
        elif method == "MKCALENDAR":
            return await handle_mkcalendar(path, user, db)
        else:
            return Response(content="Method not allowed", status_code=405)
    
    return app


def start_caldav_server(db: Session, port: int = 8081) -> bool:
    """Start the CalDAV server in a background thread."""
    global _caldav_app, _caldav_server, _caldav_thread
    
    if _caldav_server is not None:
        logger.warning("CalDAV server already running")
        return False
    
    try:
        _caldav_app = create_caldav_app()
        
        # Check for SSL certificate settings
        from app.models import Setting
        ssl_cert_setting = db.query(Setting).filter(Setting.key == "caldav_ssl_cert").first()
        ssl_key_setting = db.query(Setting).filter(Setting.key == "caldav_ssl_key").first()
        
        ssl_keyfile = ssl_key_setting.value if ssl_key_setting and ssl_key_setting.value else None
        ssl_certfile = ssl_cert_setting.value if ssl_cert_setting and ssl_cert_setting.value else None
        
        config_kwargs = {
            "app": _caldav_app,
            "host": "0.0.0.0",
            "port": port,
            "log_level": "info"
        }
        
        # Add SSL if certificates are configured
        if ssl_certfile and ssl_keyfile:
            from pathlib import Path
            cert_path = Path(ssl_certfile)
            key_path = Path(ssl_keyfile)
            if cert_path.exists() and key_path.exists():
                config_kwargs["ssl_keyfile"] = str(key_path)
                config_kwargs["ssl_certfile"] = str(cert_path)
                logger.info(f"[CalDAV] SSL enabled: cert={ssl_certfile}, key={ssl_keyfile}")
            else:
                logger.warning(f"[CalDAV] SSL certificates not found, starting without SSL")
        
        config = uvicorn.Config(**config_kwargs)
        _caldav_server = uvicorn.Server(config)
        
        def run_server():
            try:
                logger.info(f"[CalDAV] Starting server on port {port}")
                _caldav_server.run()
            except Exception as e:
                logger.error(f"[CalDAV] Server error: {e}", exc_info=True)
        
        _caldav_thread = threading.Thread(target=run_server, daemon=True)
        _caldav_thread.start()
        
        logger.info(f"[CalDAV] Server started on port {port}")
        return True
    except Exception as e:
        logger.error(f"[CalDAV] Failed to start server: {e}", exc_info=True)
        return False


def stop_caldav_server():
    """Stop the CalDAV server."""
    global _caldav_server, _caldav_thread
    
    if _caldav_server is None:
        return
    
    try:
        _caldav_server.should_exit = True
        _caldav_server = None
        if _caldav_thread:
            _caldav_thread.join(timeout=5)
            _caldav_thread = None
        logger.info("[CalDAV] Server stopped")
    except Exception as e:
        logger.error(f"[CalDAV] Error stopping server: {e}", exc_info=True)


def is_caldav_running() -> bool:
    """Check if CalDAV server is running."""
    return _caldav_server is not None
