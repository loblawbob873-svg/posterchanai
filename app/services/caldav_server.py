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
    """Handle PROPFIND request."""
    from urllib.parse import quote
    caldav_path = get_user_caldav_path(user, db)
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
            # Add default calendar
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
    
    # Individual calendar collection
    elif path == 'calendar':
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
        
        # If depth=1, list events in this calendar
        if depth == "1":
            # List all .ics files
            for ics_file in caldav_path.glob("*.ics"):
                event_uid = ics_file.stem
                items.append({
                    "href": f"{base_url}/calendar/{event_uid}.ics",
                    "props": {
                        "getcontenttype": "text/calendar; charset=utf-8",
                        "getetag": str(ics_file.stat().st_mtime)
                    }
                })
    
    # Individual event
    elif path.startswith('calendar/') and path.endswith('.ics'):
        event_uid = path.split('/')[-1].replace('.ics', '')
        ics_file = caldav_path / f"{event_uid}.ics"
        if ics_file.exists():
            items.append({
                "href": f"{base_url}/{path}",
                "props": {
                    "getcontenttype": "text/calendar; charset=utf-8",
                    "getetag": str(ics_file.stat().st_mtime)
                }
            })
    
    xml = create_caldav_response(items)
    return Response(content=xml, media_type="application/xml", status_code=207)


async def handle_report(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle REPORT request (calendar queries)."""
    from urllib.parse import quote
    body = await request.body()
    caldav_path = get_user_caldav_path(user, db)
    encoded_username = quote(user.username, safe='')
    base_url = f"/caldav/{encoded_username}"
    
    # Normalize path
    path = path.rstrip('/')
    if path.startswith(user.username):
        path = path[len(user.username):].lstrip('/')
    if path.startswith(encoded_username):
        path = path[len(encoded_username):].lstrip('/')
    
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
            
            # List matching events
            for ics_file in caldav_path.glob("*.ics"):
                try:
                    with open(ics_file, 'r', encoding='utf-8') as f:
                        ical_data = f.read()
                    
                    # Check time range if specified
                    if time_range:
                        cal = ICalendar.from_ical(ical_data.encode('utf-8'))
                        for component in cal.walk():
                            if component.name == "VEVENT":
                                dtstart = component.get('dtstart')
                                if dtstart:
                                    event_start = dtstart.dt
                                    if isinstance(event_start, datetime):
                                        if not (time_range[0] <= event_start <= time_range[1]):
                                            continue
                    
                    event_uid = ics_file.stem
                    items.append({
                        "href": f"{base_url}/calendar/{event_uid}.ics",
                        "props": {
                            "getcontenttype": "text/calendar; charset=utf-8",
                            "getetag": str(ics_file.stat().st_mtime),
                            "calendar-data": ical_data
                        }
                    })
                except Exception as e:
                    logger.debug(f"Error processing {ics_file}: {e}")
                    continue
        
        elif multiget_elem is not None:
            # Calendar multiget - get specific events by href
            hrefs = [elem.text for elem in multiget_elem.findall('.//{DAV:}href')]
            for href in hrefs:
                # Extract UID from href
                match = re.search(r'/([^/]+)\.ics$', href)
                if match:
                    event_uid = match.group(1)
                    ics_file = caldav_path / f"{event_uid}.ics"
                    if ics_file.exists():
                        try:
                            with open(ics_file, 'r', encoding='utf-8') as f:
                                ical_data = f.read()
                            items.append({
                                "href": href,
                                "props": {
                                    "getcontenttype": "text/calendar; charset=utf-8",
                                    "getetag": str(ics_file.stat().st_mtime),
                                    "calendar-data": ical_data
                                }
                            })
                        except Exception as e:
                            logger.debug(f"Error reading {ics_file}: {e}")
        
        xml = create_caldav_response(items)
        return Response(content=xml, media_type="application/xml", status_code=207)
    except Exception as e:
        logger.error(f"Error handling REPORT: {e}")
        return Response(content="", status_code=500)


async def handle_get(path: str, user: User, db: Session) -> Response:
    """Handle GET request (retrieve calendar/event)."""
    caldav_path = get_user_caldav_path(user, db)
    
    # Extract event UID from path (handle both /calendar/uid.ics and /uid.ics for backwards compat)
    match = re.search(r'/([^/]+)\.ics$', path)
    if match:
        event_uid = match.group(1)
        ics_file = caldav_path / f"{event_uid}.ics"
        if ics_file.exists():
            try:
                with open(ics_file, 'r', encoding='utf-8') as f:
                    ical_data = f.read()
                return Response(content=ical_data, media_type="text/calendar; charset=utf-8")
            except Exception as e:
                logger.error(f"Error reading event file: {e}")
                return Response(content="Error reading event", status_code=500)
    
    return Response(content="Not found", status_code=404)


async def handle_put(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle PUT request (create/update event)."""
    body = await request.body()
    caldav_path = get_user_caldav_path(user, db)
    
    try:
        ical_data = body.decode('utf-8')
        
        # Extract UID from iCalendar data
        event_uid = get_event_uid_from_ical(ical_data)
        
        # If path contains a UID, use that instead
        match = re.search(r'/([^/]+)\.ics$', path)
        if match:
            path_uid = match.group(1)
            if event_uid and event_uid != path_uid:
                logger.warning(f"UID mismatch: path={path_uid}, ical={event_uid}, using path UID")
            event_uid = path_uid
        
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
        
        # Save to file
        ics_file = caldav_path / f"{event_uid}.ics"
        with open(ics_file, 'w', encoding='utf-8') as f:
            f.write(ical_data)
        
        logger.info(f"Saved event/todo {event_uid} for user {user.username}")
        return Response(content="", status_code=201)
    except Exception as e:
        logger.error(f"Error saving event: {e}")
        return Response(content=f"Error: {e}", status_code=500)


async def handle_delete(path: str, user: User, db: Session) -> Response:
    """Handle DELETE request."""
    caldav_path = get_user_caldav_path(user, db)
    
    # Extract event UID from path (handle both /calendar/uid.ics and /uid.ics)
    match = re.search(r'/([^/]+)\.ics$', path)
    if match:
        event_uid = match.group(1)
        ics_file = caldav_path / f"{event_uid}.ics"
        if ics_file.exists():
            try:
                ics_file.unlink()
                logger.info(f"Deleted event {event_uid} for user {user.username}")
                return Response(content="", status_code=204)
            except Exception as e:
                logger.error(f"Error deleting event: {e}")
                return Response(content="Error deleting", status_code=500)
    
    return Response(content="Not found", status_code=404)


async def handle_mkcalendar(path: str, user: User, db: Session) -> Response:
    """Handle MKCALENDAR request."""
    # Calendar already exists (created on first access)
    caldav_path = get_user_caldav_path(user, db)
    caldav_path.mkdir(parents=True, exist_ok=True)
    return Response(content="", status_code=201)


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
    
    @app.api_route("/caldav/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PROPFIND", "REPORT", "MKCALENDAR"])
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
        
        # Verify user
        user = db.query(User).filter(User.username == username).first()
        if not user or not verify_password(password, user.password_hash):
            return Response(
                content="Invalid credentials",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
            )
        
        # Get depth header for PROPFIND
        depth = request.headers.get("Depth", "0")
        
        # Handle CalDAV methods
        method = request.method
        
        if method == "PROPFIND":
            return await handle_propfind(path, user, db, depth)
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
        
        config = uvicorn.Config(
            app=_caldav_app,
            host="0.0.0.0",
            port=port,
            log_level="info"
        )
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
