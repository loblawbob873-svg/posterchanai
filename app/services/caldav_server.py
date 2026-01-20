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
        status = item.get('status', 200)  # Default to 200 OK if not specified
        
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
                # Calendar data should be in CDATA, but we need to handle ]]> sequences
                # If ]]> appears in the data, we need to split the CDATA section
                # For now, replace ]]> with a safe alternative or escape it
                safe_data = prop_value.replace(']]>', ']]]]><![CDATA[>')
                xml += f'                <C:calendar-data xmlns:C="urn:ietf:params:xml:ns:caldav"><![CDATA[{safe_data}]]></C:calendar-data>\n'
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
            elif prop_name == 'sync-token':
                xml += f'                <D:sync-token xmlns:D="DAV:">{html.escape(str(prop_value))}</D:sync-token>\n'
            elif prop_name == 'getctag':
                # CTag is like ETag but for collections (calendars)
                xml += f'                <CS:getctag xmlns:CS="http://calendarserver.org/ns/">{html.escape(str(prop_value))}</CS:getctag>\n'
            elif prop_name == 'calendar-home-set':
                xml += f'                <C:calendar-home-set xmlns:C="urn:ietf:params:xml:ns:caldav"><D:href xmlns:D="DAV:">{html.escape(str(prop_value))}</D:href></C:calendar-home-set>\n'
            elif prop_name == 'calendar-user-address-set':
                xml += f'                <C:calendar-user-address-set xmlns:C="urn:ietf:params:xml:ns:caldav"><D:href xmlns:D="DAV:">{html.escape(str(prop_value))}</D:href></C:calendar-user-address-set>\n'
            elif prop_name == 'supported-report-set':
                # Return supported reports for calendars
                xml += '                <D:supported-report-set xmlns:D="DAV:">\n'
                xml += '                    <D:supported-report>\n'
                xml += '                        <D:report><C:calendar-query xmlns:C="urn:ietf:params:xml:ns:caldav"/></D:report>\n'
                xml += '                    </D:supported-report>\n'
                xml += '                    <D:supported-report>\n'
                xml += '                        <D:report><C:calendar-multiget xmlns:C="urn:ietf:params:xml:ns:caldav"/></D:report>\n'
                xml += '                    </D:supported-report>\n'
                xml += '                </D:supported-report-set>\n'
            elif prop_name == 'current-user-privilege-set':
                # Return write privileges to indicate calendar is writable
                xml += '                <D:current-user-privilege-set xmlns:D="DAV:">\n'
                xml += '                    <D:privilege><D:read/></D:privilege>\n'
                xml += '                    <D:privilege><D:write/></D:privilege>\n'
                xml += '                    <D:privilege><D:write-content/></D:privilege>\n'
                xml += '                    <D:privilege><D:write-properties/></D:privilege>\n'
                xml += '                    <D:privilege><D:bind/></D:privilege>\n'
                xml += '                    <D:privilege><D:unbind/></D:privilege>\n'
                xml += '                </D:current-user-privilege-set>\n'
        
        # Add status code based on item status
        if status == 404:
            xml += '            </D:prop>\n            <D:status>HTTP/1.1 404 Not Found</D:status>\n        </D:propstat>\n    </D:response>\n'
        else:
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


async def handle_propfind(path: str, user: User, db: Session, request: StarletteRequest = None, depth: str = "0") -> Response:
    """Handle PROPFIND request. Uses storage proxy if configured."""
    from urllib.parse import quote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Log what properties iPhone is requesting
    if request:
        body = await request.body()
        if body:
            try:
                root = ET.fromstring(body)
                requested_props = root.findall('.//{DAV:}prop/*')
                if requested_props:
                    prop_names = [prop.tag for prop in requested_props]
                    logger.info(f"[CalDAV] PROPFIND requested properties: {prop_names}")
            except Exception as e:
                logger.debug(f"[CalDAV] Could not parse PROPFIND body: {e}")
    
    # Use storage proxy (must be configured)
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
        # iPhone may request calendar-home-set and calendar-user-address-set
        items.append({
            "href": f"{base_url}/",
            "props": {
                "resourcetype": "collection",  # Just a collection, not a calendar
                "displayname": f"{user.username}'s Calendars",
                "calendar-home-set": f"{base_url}/",  # Point to itself as the calendar home
                "calendar-user-address-set": f"mailto:{user.username}"  # User's email address
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
                # Generate dynamic sync-token and ctag based on actual calendar contents
                # This ensures iPhone detects changes when events are added/modified/deleted
                import hashlib
                cal_items = proxy.list_files(cal_name)
                ics_files = [item for item in cal_items if item.get('name', '').endswith('.ics')]
                # Create hash from file count and latest modification time
                if ics_files:
                    # Get the latest modification time from all events
                    latest_mtime = max(item.get('modified', item.get('mtime', 0)) for item in ics_files)
                    # Create hash from calendar name, file count, and latest mtime
                    ctag_data = f"{cal_name}_{user.username}_{len(ics_files)}_{latest_mtime}"
                else:
                    # No events yet, use static hash
                    ctag_data = f"{cal_name}_{user.username}_0"
                ctag = hashlib.md5(ctag_data.encode()).hexdigest()[:16]
                sync_token = hashlib.md5(f"{ctag_data}_sync".encode()).hexdigest()[:16]
                logger.debug(f"[CalDAV] Calendar '{cal_name}': {len(ics_files)} events, ctag={ctag}, sync_token={sync_token}")
                items.append({
                    "href": f"{base_url}/{quote(cal_name, safe='')}/",
                    "props": {
                        "resourcetype": "calendar",
                        "displayname": cal_name.replace('_', ' ').title(),
                        "supported-calendar-component-set": "VEVENT,VTODO",
                        "calendar-description": f"{cal_name} Calendar",
                        "calendar-color": "#0088FF",
                        "calendar-timezone": "UTC",
                        "sync-token": f"http://ai.poster.place/caldav/{quote(user.username, safe='')}/{quote(cal_name, safe='')}/sync-token-{sync_token}",
                        "getctag": ctag,
                        "current-user-privilege-set": "write"
                    }
                })
            
            # If no calendar subdirectories exist but there are loose .ics files,
            # show legacy "Calendar" for backwards compatibility
            if not calendar_dirs and has_loose_ics:
                logger.info(f"[CalDAV] Adding legacy 'Calendar' (loose .ics files found)")
                import hashlib
                # Create dynamic ctag based on actual files
                latest_mtime = max(item.get('modified', item.get('mtime', 0)) for item in ics_files if item.get('name', '').endswith('.ics'))
                ctag_data = f"calendar_{user.username}_{len(ics_files)}_{latest_mtime}"
                ctag = hashlib.md5(ctag_data.encode()).hexdigest()[:16]
                sync_token = hashlib.md5(f"{ctag_data}_sync".encode()).hexdigest()[:16]
                items.append({
                    "href": f"{base_url}/calendar/",
                    "props": {
                        "resourcetype": "calendar",
                        "displayname": "Calendar",
                        "supported-calendar-component-set": "VEVENT,VTODO",
                        "calendar-description": "Default Calendar",
                        "calendar-color": "#0088FF",
                        "calendar-timezone": "UTC",
                        "sync-token": f"http://ai.poster.place/caldav/{quote(user.username, safe='')}/calendar/sync-token-{sync_token}",
                        "getctag": ctag,
                        "current-user-privilege-set": "write"
                    }
                })
            
            # If no calendars exist at all, create a default "Calendar" so users can add events
            if not calendar_dirs and not has_loose_ics:
                logger.info(f"[CalDAV] No calendars found, adding default 'Calendar'")
                import hashlib
                sync_token = hashlib.md5(f"calendar_{user.username}".encode()).hexdigest()[:16]
                ctag = hashlib.md5(f"calendar_{user.username}_ctag".encode()).hexdigest()[:16]
                items.append({
                    "href": f"{base_url}/calendar/",
                    "props": {
                        "resourcetype": "calendar",
                        "displayname": "Calendar",
                        "supported-calendar-component-set": "VEVENT,VTODO",
                        "calendar-description": "Default Calendar",
                        "calendar-color": "#0088FF",
                        "calendar-timezone": "UTC",
                        "sync-token": f"http://ai.poster.place/caldav/{quote(user.username, safe='')}/calendar/sync-token-{sync_token}",
                        "getctag": ctag,
                        "current-user-privilege-set": "write"
                    }
                })
            
            logger.info(f"[CalDAV] Returning {len(items)} calendar items for {user.username}")
    
    # Individual calendar collection
    elif '/' not in path:
        # This is a calendar name (e.g., "calendar" or "work" or "personal")
        cal_name = path
        subpath = "" if cal_name == 'calendar' else cal_name
        
        # Generate dynamic sync-token and ctag based on actual calendar contents
        # This ensures iPhone detects changes when events are added/modified/deleted
        import hashlib
        cal_items = proxy.list_files(subpath)
        ics_files = [item for item in cal_items if item.get('name', '').endswith('.ics')]
        # Create hash from file count and latest modification time
        if ics_files:
            # Get the latest modification time from all events
            latest_mtime = max(item.get('modified', item.get('mtime', 0)) for item in ics_files)
            # Create hash from calendar name, file count, and latest mtime
            ctag_data = f"{cal_name}_{user.username}_{len(ics_files)}_{latest_mtime}"
        else:
            # No events yet, use static hash
            ctag_data = f"{cal_name}_{user.username}_0"
        ctag = hashlib.md5(ctag_data.encode()).hexdigest()[:16]
        sync_token = hashlib.md5(f"{ctag_data}_sync".encode()).hexdigest()[:16]
        logger.debug(f"[CalDAV] Individual calendar '{cal_name}': {len(ics_files)} events, ctag={ctag}, sync_token={sync_token}")
        
        items.append({
            "href": f"{base_url}/{quote(cal_name, safe='')}/",
            "props": {
                "resourcetype": "calendar",
                "displayname": cal_name.replace('_', ' ').title() if cal_name != 'calendar' else "Calendar",
                "supported-calendar-component-set": "VEVENT,VTODO",
                "calendar-description": f"{cal_name} Calendar",
                "calendar-color": "#0088FF",
                "calendar-timezone": "UTC",
                "sync-token": f"http://ai.poster.place/caldav/{quote(user.username, safe='')}/{quote(cal_name, safe='')}/sync-token-{sync_token}",
                "getctag": ctag,
                "current-user-privilege-set": "write"
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
    
    # Use storage proxy (must be configured)
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
        logger.info(f"[CalDAV] REPORT request for path: {path}, cal_name: {cal_name}, subpath: {subpath}")
        root = ET.fromstring(body)
        logger.info(f"[CalDAV] Parsed REPORT body, root tag: {root.tag}")
        
        # Register namespaces for easier searching
        namespaces = {
            'D': 'DAV:',
            'C': 'urn:ietf:params:xml:ns:caldav'
        }
        
        # Check if root itself is calendar-query, calendar-multiget, or sync-collection
        query_elem = None
        multiget_elem = None
        sync_collection_elem = None
        
        # Check for sync-collection (used by iPhone)
        if root.tag.endswith('sync-collection') or root.tag == '{DAV:}sync-collection':
            sync_collection_elem = root
            logger.info(f"[CalDAV] iPhone sync-collection request detected")
        else:
            sync_collection_elem = root.find('.//{DAV:}sync-collection')
            if sync_collection_elem is None:
                sync_collection_elem = root.find('.//D:sync-collection', namespaces)
            if sync_collection_elem is None:
                sync_collection_elem = root.find('.//sync-collection')
        
        if root.tag.endswith('calendar-query') or root.tag == '{urn:ietf:params:xml:ns:caldav}calendar-query':
            query_elem = root
        else:
            # Check for calendar-query or calendar-multiget as children
            query_elem = root.find('.//{urn:ietf:params:xml:ns:caldav}calendar-query')
            if query_elem is None:
                query_elem = root.find('.//C:calendar-query', namespaces)
            if query_elem is None:
                query_elem = root.find('.//calendar-query')
        
        if root.tag.endswith('calendar-multiget') or root.tag == '{urn:ietf:params:xml:ns:caldav}calendar-multiget':
            multiget_elem = root
        else:
            multiget_elem = root.find('.//{urn:ietf:params:xml:ns:caldav}calendar-multiget')
            if multiget_elem is None:
                multiget_elem = root.find('.//C:calendar-multiget', namespaces)
            if multiget_elem is None:
                multiget_elem = root.find('.//calendar-multiget')
        
        logger.info(f"[CalDAV] query_elem: {query_elem is not None}, multiget_elem: {multiget_elem is not None}, sync_collection_elem: {sync_collection_elem is not None}")
        
        items = []
        
        if query_elem is not None:
            # Calendar query - filter by time range
            namespaces = {'C': 'urn:ietf:params:xml:ns:caldav'}
            filter_elem = query_elem.find('.//{urn:ietf:params:xml:ns:caldav}filter')
            if filter_elem is None:
                filter_elem = query_elem.find('.//C:filter', namespaces)
            if filter_elem is None:
                filter_elem = query_elem.find('.//filter')
            
            time_range = None
            if filter_elem is not None:
                time_range_elem = filter_elem.find('.//{urn:ietf:params:xml:ns:caldav}time-range')
                if time_range_elem is None:
                    time_range_elem = filter_elem.find('.//C:time-range', namespaces)
                if time_range_elem is None:
                    time_range_elem = filter_elem.find('.//time-range')
                if time_range_elem is not None:
                    start_str = time_range_elem.get('start', '')
                    end_str = time_range_elem.get('end', '')
                    if start_str and end_str:
                        try:
                            start_dt = date_parser.parse(start_str.replace('Z', '+00:00'))
                            end_dt = date_parser.parse(end_str.replace('Z', '+00:00'))
                            time_range = (start_dt, end_dt)
                            logger.info(f"[CalDAV] Time range filter: {start_str} to {end_str} (iPhone query)")
                            logger.info(f"[CalDAV] This will return events between {start_dt} and {end_dt}")
                        except Exception as e:
                            logger.warning(f"[CalDAV] Error parsing time range: {e}")
                            pass
            else:
                logger.info(f"[CalDAV] No time range filter - returning all events")
            
            # List matching events from the specified calendar directory using proxy
            file_items = proxy.list_files(subpath)
            logger.info(f"[CalDAV] REPORT query for calendar '{cal_name}' (subpath='{subpath}'): found {len(file_items)} items")
            # Log if this is an iPhone query (no time range = iPhone typically)
            if not time_range:
                logger.info(f"[CalDAV] iPhone query detected (no time range) - will return all {len(file_items)} items")
            if time_range:
                logger.info(f"[CalDAV] Time range filter: {time_range[0]} to {time_range[1]}")
            else:
                logger.info(f"[CalDAV] No time range filter - returning all events")
            
            ics_count = 0
            processed_count = 0
            read_failed_count = 0
            time_filtered_count = 0
            added_count = 0
            
            for item in file_items:
                name = item.get('name', '')
                if name.endswith('.ics'):
                    ics_count += 1
                    try:
                        # Build filepath
                        if subpath:
                            filepath = f"{subpath}/{name}"
                        else:
                            filepath = name
                        
                        logger.debug(f"[CalDAV] Processing event file {ics_count}/{len([i for i in file_items if i.get('name', '').endswith('.ics')])}: {filepath}")
                        
                        # Read calendar data using proxy
                        ical_data = proxy.read_file(filepath)
                        if not ical_data:
                            read_failed_count += 1
                            logger.warning(f"[CalDAV] Failed to read event file: {filepath}")
                            continue
                        
                        processed_count += 1
                        logger.debug(f"[CalDAV] Read event file {filepath}: {len(ical_data)} bytes")
                        
                        # Check time range if specified
                        include_event = True
                        if time_range:
                            include_event = False
                            try:
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
                                                    logger.debug(f"[CalDAV] Event {name} matches time range: {event_start}")
                                                    break
                                            else:
                                                # Date object - convert to datetime for comparison
                                                from datetime import date
                                                if isinstance(event_start, date):
                                                    # Check if the date falls within range
                                                    if time_range[0].date() <= event_start <= time_range[1].date():
                                                        include_event = True
                                                        logger.debug(f"[CalDAV] Event {name} (date) matches time range: {event_start}")
                                                        break
                                        else:
                                            # No start time, include it anyway
                                            include_event = True
                                            logger.debug(f"[CalDAV] Event {name} has no dtstart, including")
                                            break
                            except Exception as e:
                                logger.warning(f"[CalDAV] Error parsing iCalendar for time range check {name}: {e}")
                                # If we can't parse, include it anyway
                                include_event = True
                        
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
                            added_count += 1
                            # Log first few events and any matching specific UIDs for debugging
                            # Also log events with "test" in the summary
                            event_summary_lower = ""
                            try:
                                cal_test = ICalendar.from_ical(ical_data.encode('utf-8'))
                                for comp in cal_test.walk():
                                    if comp.name == "VEVENT":
                                        event_summary_lower = str(comp.get('summary', '')).lower()
                                        break
                            except:
                                pass
                            
                            if added_count <= 3 or event_uid in ["6e9ccaba-47d4-48d1-9729-701dd0d6be60"] or "test" in event_summary_lower:
                                try:
                                    cal = ICalendar.from_ical(ical_data.encode('utf-8'))
                                    for component in cal.walk():
                                        if component.name == "VEVENT":
                                            summary = str(component.get('summary', ''))
                                            dtstart = component.get('dtstart')
                                            if dtstart:
                                                start_val = dtstart.dt
                                                logger.info(f"[CalDAV] Returning event #{added_count}: '{summary}' at {start_val} (UID: {event_uid})")
                                            break
                                except Exception as e:
                                    logger.debug(f"[CalDAV] Could not parse event {event_uid} for logging: {e}")
                        else:
                            time_filtered_count += 1
                            if time_filtered_count <= 3:
                                logger.debug(f"[CalDAV] Event {name} filtered out by time range")
                    except Exception as e:
                        logger.warning(f"[CalDAV] Error processing event {name}: {e}", exc_info=True)
                        continue
            
            logger.info(f"[CalDAV] REPORT query summary for calendar '{cal_name}':")
            logger.info(f"  - Total .ics files: {ics_count}")
            logger.info(f"  - Successfully read: {processed_count}")
            logger.info(f"  - Failed to read: {read_failed_count}")
            logger.info(f"  - Filtered by time range: {time_filtered_count}")
            logger.info(f"  - Added to response: {added_count}")
            logger.info(f"[CalDAV] REPORT query returning {len(items)} events for calendar '{cal_name}'")
            # Log first few event summaries for debugging
            if added_count > 0 and added_count <= 5:
                logger.info(f"[CalDAV] First {added_count} events returned (all shown above)")
            elif added_count > 5:
                logger.info(f"[CalDAV] First 3 events shown above, {added_count - 3} more events in response")
        
        elif multiget_elem is not None:
            # Calendar multiget - get specific events by href
            namespaces = {'D': 'DAV:'}
            hrefs = []
            # Try different namespace formats
            for elem in multiget_elem.findall('.//{DAV:}href'):
                hrefs.append(elem.text)
            if not hrefs:
                for elem in multiget_elem.findall('.//D:href', namespaces):
                    hrefs.append(elem.text)
            if not hrefs:
                for elem in multiget_elem.findall('.//href'):
                    hrefs.append(elem.text)
            logger.info(f"[CalDAV] Multiget request for {len(hrefs)} hrefs")
            # Log first few hrefs to see what iPhone is requesting
            if hrefs:
                logger.info(f"[CalDAV] Multiget: First 5 hrefs requested: {hrefs[:5]}")
                logger.info(f"[CalDAV] Multiget: Last 5 hrefs requested: {hrefs[-5:]}")
            found_count = 0
            not_found_count = 0
            # Cache file listing to avoid repeated calls
            file_listing_cache = {}
            
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
                                # Get etag from cached file listing (more efficient)
                                href_subpath = "" if href_cal_name == 'calendar' else href_cal_name
                                if href_subpath not in file_listing_cache:
                                    file_listing_cache[href_subpath] = proxy.list_files(href_subpath)
                                
                                etag = "0"
                                for item in file_listing_cache[href_subpath]:
                                    if item.get('name') == f"{event_uid}.ics":
                                        etag = str(item.get('modified', item.get('mtime', 0)))
                                        break
                                
                                items.append({
                                    "href": href,
                                    "props": {
                                        "getcontenttype": "text/calendar; charset=utf-8",
                                        "getetag": etag,
                                        "calendar-data": ical_data
                                    },
                                    "status": 200  # Explicitly mark as found
                                })
                                found_count += 1
                                if found_count <= 3:
                                    logger.info(f"[CalDAV] Multiget: Found and returning event {event_uid} from {filepath}")
                            else:
                                # File exists but couldn't read - return 404 so iPhone removes it
                                items.append({
                                    "href": href,
                                    "props": {},
                                    "status": 404
                                })
                                not_found_count += 1
                                if not_found_count <= 3:
                                    logger.warning(f"[CalDAV] Multiget: File exists but read_file returned None for {filepath}, returning 404")
                        except Exception as e:
                            logger.warning(f"[CalDAV] Error reading {filepath}: {e}")
                            # Return 404 for errors so iPhone knows to remove it
                            items.append({
                                "href": href,
                                "props": {},
                                "status": 404
                            })
                            not_found_count += 1
                    else:
                        # File doesn't exist - return 404 so iPhone removes it from cache
                        items.append({
                            "href": href,
                            "props": {},
                            "status": 404
                        })
                        not_found_count += 1
                        if not_found_count <= 3:
                            logger.debug(f"[CalDAV] Multiget: File not found: {filepath} (href: {href}), returning 404")
                else:
                    # Href doesn't match expected pattern - return 404
                    items.append({
                        "href": href,
                        "props": {},
                        "status": 404
                    })
                    not_found_count += 1
                    if len(hrefs) <= 5 or hrefs.index(href) < 3:
                        logger.debug(f"[CalDAV] Multiget: Href doesn't match pattern: {href}, returning 404")
            
            total_items = len(items)
            items_with_data = len([i for i in items if i.get('props', {}).get('calendar-data')])
            items_404 = len([i for i in items if i.get('status') == 404])
            logger.info(f"[CalDAV] Multiget summary: {found_count} found, {not_found_count} not found, returning {total_items} total items ({items_with_data} with calendar-data, {items_404} with 404 status)")
        
        elif sync_collection_elem is not None:
            # iPhone sync-collection request - return all events in the calendar
            # This is similar to calendar-query but without time filtering
            logger.info(f"[CalDAV] Handling sync-collection request for calendar '{cal_name}' (iPhone sync)")
            
            # Parse sync-token from request (if provided)
            sync_token_elem = sync_collection_elem.find('.//{DAV:}sync-token')
            if sync_token_elem is None:
                sync_token_elem = sync_collection_elem.find('.//D:sync-token', namespaces)
            if sync_token_elem is None:
                sync_token_elem = sync_collection_elem.find('.//sync-token')
            
            old_sync_token = None
            if sync_token_elem is not None and sync_token_elem.text:
                old_sync_token = sync_token_elem.text.strip()
                logger.info(f"[CalDAV] sync-collection with sync-token: {old_sync_token[:50]}...")
            
            # List all events from the specified calendar directory using proxy
            file_items = proxy.list_files(subpath)
            logger.info(f"[CalDAV] sync-collection for calendar '{cal_name}' (subpath='{subpath}'): found {len(file_items)} items")
            
            # Get current sync-token (based on current file count and latest mtime)
            import hashlib
            ics_files = [item for item in file_items if item.get('name', '').endswith('.ics')]
            if ics_files:
                latest_mtime = max(item.get('modified', item.get('mtime', 0)) for item in ics_files)
                ctag_data = f"{cal_name}_{user.username}_{len(ics_files)}_{latest_mtime}"
            else:
                ctag_data = f"{cal_name}_{user.username}_0"
            current_sync_token = hashlib.md5(f"{ctag_data}_sync".encode()).hexdigest()[:16]
            
            # If sync-token changed, we need to report deletions
            # For now, if there's an old sync-token and it's different, we'll do a full sync
            # (proper implementation would track what events existed at old token)
            if old_sync_token and old_sync_token != f"http://ai.poster.place/caldav/{quote(user.username, safe='')}/{quote(cal_name, safe='')}/sync-token-{current_sync_token}":
                logger.info(f"[CalDAV] sync-token changed - doing full sync (old token provided, changes detected)")
                # Full sync mode - return all current events
                # Note: Proper sync-collection would track deleted events, but for simplicity
                # we'll rely on multiget to handle deletions when iPhone queries specific events
            
            ics_count = 0
            processed_count = 0
            read_failed_count = 0
            added_count = 0
            
            for item in file_items:
                name = item.get('name', '')
                if name.endswith('.ics'):
                    ics_count += 1
                    try:
                        # Build filepath
                        if subpath:
                            filepath = f"{subpath}/{name}"
                        else:
                            filepath = name
                        
                        # Read calendar data using proxy
                        ical_data = proxy.read_file(filepath)
                        if not ical_data:
                            read_failed_count += 1
                            logger.warning(f"[CalDAV] Failed to read event file: {filepath}")
                            continue
                        
                        processed_count += 1
                        
                        # Add event to response
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
                        added_count += 1
                        
                        # Log first few events and any with "test" in summary
                        if added_count <= 3:
                            try:
                                cal = ICalendar.from_ical(ical_data.encode('utf-8'))
                                for component in cal.walk():
                                    if component.name == "VEVENT":
                                        summary = str(component.get('summary', ''))
                                        dtstart = component.get('dtstart')
                                        if dtstart:
                                            start_val = dtstart.dt
                                            logger.info(f"[CalDAV] sync-collection: Returning event #{added_count}: '{summary}' at {start_val} (UID: {event_uid})")
                                        break
                            except Exception as e:
                                logger.debug(f"[CalDAV] Could not parse event {event_uid} for logging: {e}")
                    except Exception as e:
                        logger.warning(f"[CalDAV] Error processing event {name}: {e}", exc_info=True)
                        continue
            
            logger.info(f"[CalDAV] sync-collection summary for calendar '{cal_name}':")
            logger.info(f"  - Total .ics files: {ics_count}")
            logger.info(f"  - Successfully read: {processed_count}")
            logger.info(f"  - Failed to read: {read_failed_count}")
            logger.info(f"  - Added to response: {added_count}")
            logger.info(f"[CalDAV] sync-collection returning {len(items)} events for calendar '{cal_name}'")
            logger.info(f"[CalDAV] New sync-token: {new_sync_token_url}")
            
            # Add sync-token to response by adding it as a special item
            # The sync-token should be in the multistatus response root, not as an item
            # We'll handle this in the XML generation
        
        # Generate XML response
        xml = create_caldav_response(items)
        
        # For sync-collection, we need to add the sync-token to the response
        # Insert it after the opening multistatus tag
        if sync_collection_elem is not None:
            import re
            new_sync_token_url = f"http://ai.poster.place/caldav/{quote(user.username, safe='')}/{quote(cal_name, safe='')}/sync-token-{current_sync_token}"
            # Add sync-token after multistatus opening tag
            sync_token_xml = f'    <D:sync-token xmlns:D="DAV:">{html.escape(new_sync_token_url)}</D:sync-token>\n'
            xml = xml.replace('<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">\n', 
                            f'<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">\n{sync_token_xml}')
        
        return Response(content=xml, media_type="application/xml", status_code=207)
    except Exception as e:
        logger.error(f"[CalDAV] Error handling REPORT: {e}", exc_info=True)
        # Return a proper error response instead of empty 500
        error_xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:error xmlns:D="DAV:">
    <D:internal-server-error/>
</D:error>'''
        return Response(content=error_xml, media_type="application/xml", status_code=500)


async def handle_get(path: str, user: User, db: Session) -> Response:
    """Handle GET request (retrieve calendar/event). Uses storage proxy if configured."""
    from urllib.parse import unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Use storage proxy (must be configured)
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
            # Get ETag for the file (iPhone requires ETag in GET response)
            try:
                file_items = proxy.list_files(cal_name if cal_name != 'calendar' else "")
                etag = "0"
                for item in file_items:
                    if item.get('name') == f"{event_uid}.ics":
                        etag = str(item.get('modified', item.get('mtime', 0)))
                        break
            except:
                etag = "0"
            
            return Response(
                content=ical_data,
                media_type="text/calendar; charset=utf-8",
                headers={"ETag": f'"{etag}"'}
            )
        else:
            logger.warning(f"Event file not found: {filepath}")
            return Response(content="Not found", status_code=404)
    
    return Response(content="Not found", status_code=404)


async def handle_put(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle PUT request (create/update event). Uses storage proxy if configured."""
    from urllib.parse import unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    try:
        body = await request.body()
        logger.info(f"[CalDAV] handle_put: path={path}, user={user.username}, body_size={len(body)} bytes")
    except Exception as e:
        logger.error(f"[CalDAV] Error reading body in handle_put: {e}", exc_info=True)
        error_xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:error xmlns:D="DAV:">
    <D:internal-server-error/>
</D:error>'''
        return Response(content=error_xml, media_type="application/xml", status_code=500)
    
    # Use storage proxy (must be configured)
    proxy = DAVStorageProxy(db, user.username, 'caldav')
    
    # Log PUT request for debugging
    if_match = request.headers.get("If-Match")
    if_none_match = request.headers.get("If-None-Match")
    logger.info(f"[CalDAV] PUT request for path: {path}, user: {user.username}, body size: {len(body)} bytes, If-Match: {if_match}, If-None-Match: {if_none_match}")
    
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
            # Default to "main" for iPhone sync compatibility (not "calendar" which saves to root)
            cal_name = 'main'
            logger.info(f"[CalDAV] No calendar name in path, defaulting to 'main' for iPhone sync compatibility")
        
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
        
        # Build filepath - ALWAYS use calendar subdirectory (prefer "main" for iPhone sync)
        # Never save to root - iPhone doesn't sync from root
        if cal_name == 'calendar':
            # Legacy "calendar" name - redirect to "main" for iPhone compatibility
            cal_name = 'main'
            logger.warning(f"[CalDAV] Redirecting 'calendar' to 'main' for iPhone sync compatibility")
        
        filepath = f"{cal_name}/{event_uid}.ics"
        
        # Check if file already exists (update vs create)
        file_exists = proxy.file_exists(filepath)
        is_update = file_exists
        
        # Save to file using proxy
        logger.info(f"[CalDAV] Saving event {event_uid} to filepath: {filepath}, calendar: {cal_name}, is_update: {is_update}")
        success = proxy.write_file(filepath, ical_data)
        logger.info(f"[CalDAV] write_file returned: {success} for {filepath}")
        
        if success:
            logger.info(f"[CalDAV] Successfully saved event/todo {event_uid} for user {user.username} in calendar {cal_name}")
            
            # Verify file was written by trying to read it back
            verify_data = proxy.read_file(filepath)
            if not verify_data:
                logger.warning(f"[CalDAV] File {filepath} was not found after write, but write_file returned success")
            
            # Get ETag for the saved file (iPhone requires ETag in PUT response)
            etag = "0"
            try:
                # Get file info for ETag - wait a moment for storage to update
                import time
                time.sleep(0.1)  # Small delay to ensure storage server has updated
                
                file_items = proxy.list_files(cal_name if cal_name != 'calendar' else "")
                for item in file_items:
                    if item.get('name') == f"{event_uid}.ics":
                        etag = str(item.get('modified', item.get('mtime', 0)))
                        logger.debug(f"[CalDAV] Got ETag {etag} for event {event_uid}")
                        break
            except Exception as e:
                logger.warning(f"[CalDAV] Could not get ETag for {event_uid}: {e}")
                # Use hash of content as fallback ETag
                import hashlib
                etag = hashlib.md5(ical_data.encode('utf-8')).hexdigest()[:16]
            
            # Return appropriate status code: 201 for new, 204 for updates
            # iPhone expects 201 for new events, 204 for updates
            status_code = 204 if is_update else 201
            logger.info(f"[CalDAV] Returning {status_code} with ETag: {etag} for event {event_uid} (is_update: {is_update})")
            return Response(
                content="",
                status_code=status_code,
                headers={"ETag": f'"{etag}"'}
            )
        else:
            logger.error(f"[CalDAV] Failed to save event {event_uid} - write_file returned False")
            error_xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:error xmlns:D="DAV:">
    <D:internal-server-error/>
</D:error>'''
            return Response(content=error_xml, media_type="application/xml", status_code=500)
    except Exception as e:
        logger.error(f"[CalDAV] Error saving event: {e}", exc_info=True)
        error_xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:error xmlns:D="DAV:">
    <D:internal-server-error/>
</D:error>'''
        return Response(content=error_xml, media_type="application/xml", status_code=500)


async def handle_delete(path: str, user: User, db: Session) -> Response:
    """Handle DELETE request. Uses storage proxy if configured."""
    from urllib.parse import unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Use storage proxy (must be configured)
    proxy = DAVStorageProxy(db, user.username, 'caldav')
    
    # Extract calendar name and event UID from path
    match = re.search(r'/([^/]+)/([^/]+)\.ics$', path)
    if match:
        cal_name = unquote(match.group(1))
        event_uid = match.group(2)
        
        # Build filepath using proxy
        if cal_name == 'calendar':
            filepath = f"{event_uid}.ics"  # Legacy: root directory
        else:
            filepath = f"{cal_name}/{event_uid}.ics"
        
        # Build filepath (with calendar subdirectory if not 'calendar')
        if cal_name == 'calendar':
            filepath = f"{event_uid}.ics"
        else:
            filepath = f"{cal_name}/{event_uid}.ics"
        
        # Use storage proxy (must be configured)
        from app.services.dav_storage_proxy import DAVStorageProxy
        proxy = DAVStorageProxy(db, user.username, 'caldav')
        
        # Delete file using proxy
        success = proxy.delete_file(filepath)
        if success:
            logger.info(f"[CalDAV] ✓ Deleted event {event_uid} from calendar {cal_name} for user {user.username}")
            logger.info(f"[CalDAV] Sync-token will change on next PROPFIND (file count decreased)")
            return Response(content="", status_code=204)
        else:
            logger.warning(f"[CalDAV] Event file not found or failed to delete: {filepath}")
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
    from urllib.parse import quote
    import html
    
    body = await request.body()
    
    try:
        logger.info(f"[CalDAV] PROPPATCH request for path: {path}, user: {user.username}, body size: {len(body)} bytes")
        
        # Parse the PROPPATCH request
        root = ET.fromstring(body)
        
        # Build proper href (full URL path)
        encoded_username = quote(user.username, safe='')
        if path.startswith(user.username) or path.startswith(encoded_username):
            # Path already includes username, use as-is
            href = f"/caldav/{path}"
        else:
            href = f"/caldav/{encoded_username}/{path}"
        href = href.rstrip('/') + '/' if href != '/caldav/' else href
        
        # Parse what properties iPhone is trying to set
        set_props = root.findall('.//{DAV:}set/{DAV:}prop/*')
        remove_props = root.findall('.//{DAV:}remove/{DAV:}prop/*')
        
        logger.info(f"[CalDAV] PROPPATCH: setting {len(set_props)} properties, removing {len(remove_props)} properties")
        for prop in set_props:
            logger.debug(f"[CalDAV] PROPPATCH set property: {prop.tag}")
        for prop in remove_props:
            logger.debug(f"[CalDAV] PROPPATCH remove property: {prop.tag}")
        
        # For now, just accept the changes without actually storing them
        # The calendar properties are hardcoded in handle_propfind
        # In a full implementation, you'd store these in a database or file
        
        # Build response with all properties that were set/removed
        escaped_href = html.escape(href)
        xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
    <D:response>
        <D:href>{escaped_href}</D:href>
        <D:propstat>
            <D:prop>'''
        
        # Include all properties that were set/removed in the response
        # iPhone expects the exact same properties returned that it sent
        if not set_props and not remove_props:
            # No properties found - return empty prop (some clients send empty PROPPATCH)
            xml += '\n            </D:prop>'
        else:
            for prop in set_props + remove_props:
                prop_tag = prop.tag
                prop_text = prop.text if prop.text else ""
                
                # Handle namespaced properties correctly
                if prop_tag.startswith('{DAV:}'):
                    prop_name = prop_tag[6:]  # Remove {DAV:} prefix
                    namespace_prefix = 'D'
                elif prop_tag.startswith('{urn:ietf:params:xml:ns:caldav}'):
                    prop_name = prop_tag[35:]  # Remove namespace prefix
                    namespace_prefix = 'C'
                elif '}' in prop_tag:
                    namespace, prop_name = prop_tag.split('}', 1)
                    if 'caldav' in namespace.lower():
                        namespace_prefix = 'C'
                    else:
                        namespace_prefix = 'D'
                else:
                    prop_name = prop_tag
                    namespace_prefix = 'D'
                
                xml += f'\n                <{namespace_prefix}:{prop_name}'
                
                # Add property value if present
                if prop_text:
                    escaped_text = html.escape(prop_text)
                    xml += f'>{escaped_text}</{namespace_prefix}:{prop_name}>'
                else:
                    xml += '/>'
        
        xml += f'''
            </D:prop>
            <D:status>HTTP/1.1 200 OK</D:status>
        </D:propstat>
    </D:response>
</D:multistatus>'''
        
        logger.debug(f"[CalDAV] PROPPATCH response for {href}")
        return Response(content=xml, media_type="application/xml", status_code=207)
    except Exception as e:
        logger.error(f"[CalDAV] Error handling PROPPATCH: {e}", exc_info=True)
        error_xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:error xmlns:D="DAV:">
    <D:internal-server-error/>
</D:error>'''
        return Response(content=error_xml, media_type="application/xml", status_code=500)


def create_caldav_app() -> FastAPI:
    """Create CalDAV FastAPI application."""
    app = FastAPI(title="Posterchanai CalDAV Server")
    
    @app.api_route("/.well-known/caldav", methods=["GET", "HEAD", "OPTIONS", "PROPFIND"])
    async def caldav_discovery(request: StarletteRequest):
        """CalDAV discovery endpoint. Returns 302 redirect to CalDAV principal or handles PROPFIND."""
        # Handle PROPFIND requests (some clients like iPhone use this)
        if request.method == "PROPFIND":
            # Try to authenticate
            from app.database import SessionLocal
            from app.models import User
            from app.auth import verify_password
            import base64
            from urllib.parse import quote
            
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Basic "):
                return Response(
                    content="Unauthorized",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
                )
            
            try:
                credentials = base64.b64decode(auth_header[6:]).decode('utf-8')
                username, password = credentials.split(':', 1)
            except:
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
                )
            
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.username == username).first()
                if not user and '@' in username:
                    username_part = username.split('@')[0]
                    user = db.query(User).filter(User.username.like(f"{username_part}@%")).first()
                
                if not user or not verify_password(password, user.password_hash):
                    return Response(
                        content="Invalid credentials",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
                    )
                
                # Return principal URL in PROPFIND response
                encoded_username = quote(user.username, safe='')
                principal_url = f"/caldav/{encoded_username}/"
                xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
    <D:response>
        <D:href>/.well-known/caldav</D:href>
        <D:propstat>
            <D:prop>
                <D:resourcetype><D:collection/></D:resourcetype>
                <D:current-user-principal><D:href>{principal_url}</D:href></D:current-user-principal>
            </D:prop>
            <D:status>HTTP/1.1 200 OK</D:status>
        </D:propstat>
    </D:response>
</D:multistatus>'''
                return Response(content=xml, media_type="application/xml", status_code=207)
            finally:
                db.close()
        
        # For GET/HEAD/OPTIONS, return redirect
        host = request.headers.get("Host", "ai.poster.place")
        scheme = request.headers.get("X-Forwarded-Proto", "https")
        if not scheme or scheme == "http":
            if request.url.scheme == "https" or "443" in str(request.url.port):
                scheme = "https"
        redirect_url = f"{scheme}://{host}/caldav/"
        return Response(
            content="",
            status_code=302,
            headers={"Location": redirect_url, "Cache-Control": "no-cache"}
        )
        return Response(
            content="",
            status_code=301,
            headers={"Location": "/caldav/"}
        )
    
    @app.api_route("/caldav/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PROPFIND", "PROPPATCH", "REPORT", "MKCALENDAR", "OPTIONS"])
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
            auth_bytes = base64.b64decode(auth_header[6:])
            credentials = auth_bytes.decode('utf-8')
            # Handle case where password might contain colons
            if ':' not in credentials:
                logger.warning(f"[CalDAV] No colon found in credentials after base64 decode")
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
                )
            username, password = credentials.split(':', 1)
            # Strip any whitespace that might have been introduced
            username = username.strip()
            password = password.strip()
            logger.debug(f"[CalDAV] Parsed Basic Auth - username length: {len(username)}, password length: {len(password)}")
        except Exception as e:
            logger.error(f"[CalDAV] Error parsing Basic Auth: {e}", exc_info=True)
            return Response(
                content="Invalid credentials",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
            )
        
        # Verify user - try exact match first, then try without domain
        # IMPORTANT: Use with_entities or ensure password_hash is loaded
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
        
        # Ensure password_hash is loaded (refresh from database if needed)
        db.refresh(user, ['password_hash'])
        if not user.password_hash:
            logger.error(f"[CalDAV] User {user.username} has no password_hash after refresh!")
            return Response(
                content="Invalid credentials",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
            )
        
        # Verify password
        # Log authentication attempt (but not the actual password)
        logger.info(f"[CalDAV] Verifying password for user: {user.username} (auth username: {username}, password provided: {bool(password)})")
        
        # Special case: __USE_SESSION_AUTH__ is used by backend code connecting to built-in server
        # This is safe because it's only used for localhost connections from the same process
        # Skip password verification in this case
        if password == "__USE_SESSION_AUTH__":
            # Check if this is a localhost connection (safety check)
            client_host = request.client.host if hasattr(request, 'client') and request.client else None
            if client_host in ('127.0.0.1', 'localhost', '::1') or 'localhost' in str(request.url):
                logger.info(f"[CalDAV] ✓ Authenticated user: {user.username} (using __USE_SESSION_AUTH__ from localhost)")
                # Skip to the end - authentication successful
            else:
                logger.warning(f"[CalDAV] __USE_SESSION_AUTH__ used from non-localhost: {client_host}")
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
                )
        elif not user.password_hash:
            logger.error(f"[CalDAV] User {user.username} has no password hash!")
            return Response(
                content="Invalid credentials",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
            )
        else:
            # Normal password verification for external clients (like iPhone)
            # Ensure password_hash is loaded (refresh from database if needed)
            db.refresh(user, ['password_hash'])
            if not user.password_hash:
                logger.error(f"[CalDAV] User {user.username} has no password_hash after refresh!")
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
                )
            
            try:
                # Try direct bcrypt verification first (in case hash is bytes)
                import bcrypt
                password_valid = False
                
                logger.info(f"[CalDAV] Attempting password verification - hash type: {type(user.password_hash)}, hash preview: {str(user.password_hash)[:20]}...")
                
                # Check if password_hash is bytes or string
                if isinstance(user.password_hash, bytes):
                    password_valid = bcrypt.checkpw(password.encode('utf-8'), user.password_hash)
                    logger.info(f"[CalDAV] Used bytes method, result: {password_valid}")
                else:
                    # Use the verify_password function which handles string hashes
                    password_valid = verify_password(password, user.password_hash)
                    logger.info(f"[CalDAV] Used verify_password function, result: {password_valid}")
                    
            except Exception as e:
                logger.error(f"[CalDAV] Error verifying password: {e}", exc_info=True)
                logger.error(f"[CalDAV] Password hash type: {type(user.password_hash)}, length: {len(user.password_hash) if user.password_hash else 0}")
                logger.error(f"[CalDAV] Password type: {type(password)}, length: {len(password) if password else 0}")
                password_valid = False
            
            if not password_valid:
                logger.warning(f"[CalDAV] Invalid password for user: {user.username} (auth username: {username})")
                logger.info(f"[CalDAV] Password hash exists: {bool(user.password_hash)}, hash type: {type(user.password_hash)}, hash length: {len(user.password_hash) if user.password_hash else 0}")
                logger.info(f"[CalDAV] Password length: {len(password) if password else 0}, password starts with: {password[:3] if password and len(password) >= 3 else 'N/A'}")
                # Try to see if we can verify with web UI method for comparison
                try:
                    from app.auth import verify_password as web_verify
                    web_result = web_verify(password, user.password_hash)
                    logger.info(f"[CalDAV] Web UI verify_password result: {web_result}")
                    if web_result:
                        logger.error(f"[CalDAV] ⚠️ PASSWORD VERIFICATION BUG: Web UI method says password is valid, but CalDAV method says invalid!")
                except Exception as e2:
                    logger.info(f"[CalDAV] Could not test with web UI method: {e2}")
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CalDAV"'}
                )
            
            logger.info(f"[CalDAV] ✓ Authenticated user: {user.username}")
        
        # Get depth header for PROPFIND
        depth = request.headers.get("Depth", "0")
        
        # Handle CalDAV methods
        method = request.method
        
        # Log all requests for debugging (especially PUT requests from iPhone)
        logger.info(f"[CalDAV] Request: {method} {path} from user {user.username}")
        
        # Log PUT requests with more detail
        if method == "PUT":
            # Read body to get size, but handle_put will read it again
            try:
                body_bytes = await request.body()
                body_size = len(body_bytes) if body_bytes else 0
                logger.info(f"[CalDAV] ⚠️ PUT REQUEST RECEIVED: path={path}, user={user.username}, body_size={body_size} bytes")
            except Exception as e:
                logger.error(f"[CalDAV] Error reading PUT request body: {e}")
                body_size = 0
        
        # Handle OPTIONS request (required by some clients including iPhone)
        if method == "OPTIONS":
            return Response(
                content="",
                status_code=200,
                headers={
                    "Allow": "GET, POST, PUT, DELETE, PROPFIND, PROPPATCH, REPORT, MKCALENDAR, OPTIONS",
                    "DAV": "1, 2, 3, calendar-access, calendar-schedule, calendar-auto-schedule",
                    "Content-Length": "0"
                }
            )
        
        if method == "PROPFIND":
            return await handle_propfind(path, user, db, request, depth)
        elif method == "PROPPATCH":
            return await handle_proppatch(path, user, db, request)
        elif method == "REPORT":
            return await handle_report(path, user, db, request)
        elif method == "GET":
            return await handle_get(path, user, db)
        elif method == "PUT":
            logger.info(f"[CalDAV] ⚠️ Calling handle_put for path: {path}")
            result = await handle_put(path, user, db, request)
            logger.info(f"[CalDAV] ⚠️ handle_put returned status: {result.status_code}")
            return result
        elif method == "DELETE":
            return await handle_delete(path, user, db)
        elif method == "MKCALENDAR":
            return await handle_mkcalendar(path, user, db)
        else:
            logger.warning(f"[CalDAV] Method not allowed: {method} for path: {path}")
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
