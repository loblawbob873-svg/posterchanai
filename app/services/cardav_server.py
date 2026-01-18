"""
Built-in CardDAV Server - Serves contacts via CardDAV protocol.
Full implementation supporting all contacts commands.
"""
import logging
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, List, Dict
from sqlalchemy.orm import Session
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.requests import Request as StarletteRequest
import uvicorn
import uuid
import base64
import re

from app.models import User
from app.services.storage_service import get_storage_service
from app.auth import verify_password
from app.database import get_db, SessionLocal
import vobject

logger = logging.getLogger(__name__)

# Global server instance
_cardav_app: Optional[FastAPI] = None
_cardav_server: Optional[uvicorn.Server] = None
_cardav_thread: Optional[threading.Thread] = None


def get_user_cardav_path(user: User, db: Session) -> Path:
    """Get the CardDAV storage path for a user."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(user.username)
    cardav_path = user_path / "carddav"
    cardav_path.mkdir(parents=True, exist_ok=True)
    return cardav_path


def create_cardav_response(multistatus_items: List[Dict]) -> str:
    """Create a CardDAV multistatus XML response."""
    import html
    xml = '''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
'''
    for item in multistatus_items:
        href = html.escape(item.get('href', ''))
        props = item.get('props', {})
        xml += f'    <D:response>\n        <D:href>{href}</D:href>\n        <D:propstat>\n            <D:prop>\n'
        for prop_name, prop_value in props.items():
            if prop_name == 'resourcetype':
                if prop_value == 'addressbook':
                    xml += '                <D:resourcetype><D:collection/><C:addressbook xmlns:C="urn:ietf:params:xml:ns:carddav"/></D:resourcetype>\n'
                else:
                    xml += f'                <D:resourcetype><D:collection/></D:resourcetype>\n'
            elif prop_name == 'displayname':
                xml += f'                <D:displayname>{html.escape(str(prop_value))}</D:displayname>\n'
            elif prop_name == 'getcontenttype':
                xml += f'                <D:getcontenttype>{html.escape(str(prop_value))}</D:getcontenttype>\n'
            elif prop_name == 'getetag':
                xml += f'                <D:getetag>"{html.escape(str(prop_value))}"</D:getetag>\n'
            elif prop_name == 'address-data':
                # Use CDATA for vCard data to avoid XML escaping issues
                xml += f'                <C:address-data xmlns:C="urn:ietf:params:xml:ns:carddav"><![CDATA[{prop_value}]]></C:address-data>\n'
            elif prop_name == 'current-user-principal':
                # Principal URL for DAV discovery (used by iPhone)
                principal_href = html.escape(str(prop_value))
                xml += f'                <D:current-user-principal><D:href>{principal_href}</D:href></D:current-user-principal>\n'
            elif prop_name == 'addressbook-home-set':
                xml += f'                <C:addressbook-home-set xmlns:C="urn:ietf:params:xml:ns:carddav"><D:href xmlns:D="DAV:">{html.escape(str(prop_value))}</D:href></C:addressbook-home-set>\n'
            elif prop_name == 'sync-token':
                xml += f'                <D:sync-token xmlns:D="DAV:">{html.escape(str(prop_value))}</D:sync-token>\n'
            elif prop_name == 'getctag':
                # CTag is like ETag but for collections (addressbooks)
                xml += f'                <CS:getctag xmlns:CS="http://calendarserver.org/ns/">{html.escape(str(prop_value))}</CS:getctag>\n'
        xml += '            </D:prop>\n            <D:status>HTTP/1.1 200 OK</D:status>\n        </D:propstat>\n    </D:response>\n'
    xml += '</D:multistatus>'
    return xml


def get_contact_uid_from_vcard(vcard_data: str) -> Optional[str]:
    """Extract UID from vCard data."""
    try:
        vcard = vobject.readOne(vcard_data)
        if hasattr(vcard, 'uid'):
            return str(vcard.uid.value)
    except Exception as e:
        logger.debug(f"Error parsing vCard for UID: {e}")
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
                    logger.info(f"[CardDAV] PROPFIND requested properties: {prop_names}")
            except Exception as e:
                logger.debug(f"[CardDAV] Could not parse PROPFIND body: {e}")
    
    # Use storage proxy (must be configured)
    proxy = DAVStorageProxy(db, user.username, 'cardav')
    encoded_username = quote(user.username, safe='')
    base_url = f"/carddav/{encoded_username}"
    
    items = []
    
    # Normalize path
    path = path.rstrip('/')
    if path.startswith(user.username):
        path = path[len(user.username):].lstrip('/')
    if path.startswith(encoded_username):
        path = path[len(encoded_username):].lstrip('/')
    
    # Root addressbook home - container for addressbooks
    # When path is empty, this is the root /carddav/ endpoint
    # iPhone expects this to return the principal URL
    if not path or path == '':
        # Return the principal URL (user's addressbook home)
        # iPhone needs addressbook-home-set property
        items.append({
            "href": f"{base_url}/",
            "props": {
                "resourcetype": "collection",
                "displayname": f"{user.username}'s Addressbooks",
                "current-user-principal": f"{base_url}/",  # Principal URL for iPhone
                "addressbook-home-set": f"{base_url}/"  # Addressbook home set for iPhone
            }
        })
        
        # If depth=1, list addressbooks
        if depth == "1":
            # List files using proxy
            file_items = proxy.list_files("")
            
            # Check for addressbook subdirectories
            addressbook_dirs = []
            vcf_files = []
            for item in file_items:
                if item.get('is_directory', False) and not item.get('name', '').startswith('.'):
                    addressbook_dirs.append(item.get('name'))
                elif item.get('name', '').endswith('.vcf'):
                    vcf_files.append(item.get('name'))
            
            # If no subdirectories, check for loose .vcf files (legacy mode)
            has_loose_vcf = len(vcf_files) > 0
            
            if not addressbook_dirs and has_loose_vcf:
                # Legacy mode: show root as default addressbook
                # iPhone needs sync-token and getctag for addressbooks
                import hashlib
                sync_token = hashlib.md5(f"contacts_{user.username}".encode()).hexdigest()[:16]
                ctag = hashlib.md5(f"contacts_{user.username}_ctag".encode()).hexdigest()[:16]
                items.append({
                    "href": f"{base_url}/contacts/",
                    "props": {
                        "resourcetype": "addressbook",
                        "displayname": "Contacts",
                        "sync-token": f"http://ai.poster.place/carddav/{quote(user.username, safe='')}/contacts/sync-token-{sync_token}",
                        "getctag": ctag
                    }
                })
            else:
                # New mode: show actual addressbook subdirectories
                for abook_name in sorted(addressbook_dirs):
                    # iPhone needs sync-token and getctag for addressbooks
                    import hashlib
                    sync_token = hashlib.md5(f"{abook_name}_{user.username}".encode()).hexdigest()[:16]
                    ctag = hashlib.md5(f"{abook_name}_{user.username}_ctag".encode()).hexdigest()[:16]
                    items.append({
                        "href": f"{base_url}/{quote(abook_name, safe='')}/",
                        "props": {
                            "resourcetype": "addressbook",
                            "displayname": abook_name.replace('_', ' ').title(),
                            "sync-token": f"http://ai.poster.place/carddav/{quote(user.username, safe='')}/{quote(abook_name, safe='')}/sync-token-{sync_token}",
                            "getctag": ctag
                        }
                    })
    
    # Individual addressbook
    elif '/' not in path:
        abook_name = path
        subpath = "" if abook_name == 'contacts' else abook_name
        
        # iPhone needs sync-token and getctag for addressbooks
        import hashlib
        if abook_name == 'contacts':
            sync_token = hashlib.md5(f"contacts_{user.username}".encode()).hexdigest()[:16]
            ctag = hashlib.md5(f"contacts_{user.username}_ctag".encode()).hexdigest()[:16]
        else:
            sync_token = hashlib.md5(f"{abook_name}_{user.username}".encode()).hexdigest()[:16]
            ctag = hashlib.md5(f"{abook_name}_{user.username}_ctag".encode()).hexdigest()[:16]
        
        items.append({
            "href": f"{base_url}/{quote(abook_name, safe='')}/",
            "props": {
                "resourcetype": "addressbook",
                "displayname": abook_name.replace('_', ' ').title() if abook_name != 'contacts' else "Contacts",
                "sync-token": f"http://ai.poster.place/carddav/{quote(user.username, safe='')}/{quote(abook_name, safe='')}/sync-token-{sync_token}",
                "getctag": ctag
            }
        })
        
        # If depth=1, list contacts
        if depth == "1":
            file_items = proxy.list_files(subpath)
            for item in file_items:
                name = item.get('name', '')
                if name.endswith('.vcf'):
                    contact_uid = name.replace('.vcf', '')
                    etag = str(item.get('modified', item.get('mtime', 0)))
                    items.append({
                        "href": f"{base_url}/{quote(abook_name, safe='')}/{contact_uid}.vcf",
                        "props": {
                            "getcontenttype": "text/vcard; charset=utf-8",
                            "getetag": etag
                        }
                    })
    
    # Individual contact
    elif path.count('/') == 1 and path.endswith('.vcf'):
        parts = path.split('/')
        abook_name = parts[0]
        contact_file = parts[1]
        contact_uid = contact_file.replace('.vcf', '')
        
        # Build filepath
        if abook_name == 'contacts':
            filepath = f"{contact_uid}.vcf"
        else:
            filepath = f"{abook_name}/{contact_uid}.vcf"
        
        # Check if file exists using proxy
        if proxy.file_exists(filepath):
            # Get file info for etag
            file_items = proxy.list_files(abook_name if abook_name != 'contacts' else "")
            etag = "0"
            for item in file_items:
                if item.get('name') == f"{contact_uid}.vcf":
                    etag = str(item.get('modified', item.get('mtime', 0)))
                    break
            
            items.append({
                "href": f"{base_url}/{path}",
                "props": {
                    "getcontenttype": "text/vcard; charset=utf-8",
                    "getetag": etag
                }
            })
    
    xml = create_cardav_response(items)
    return Response(content=xml, media_type="application/xml", status_code=207)


async def handle_report(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle REPORT request (contact queries). Uses storage proxy if configured."""
    from urllib.parse import quote, unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    body = await request.body()
    
    # Use storage proxy (must be configured)
    proxy = DAVStorageProxy(db, user.username, 'cardav')
    encoded_username = quote(user.username, safe='')
    base_url = f"/carddav/{encoded_username}"
    
    # Normalize path
    path = path.rstrip('/')
    if path.startswith(user.username):
        path = path[len(user.username):].lstrip('/')
    if path.startswith(encoded_username):
        path = path[len(encoded_username):].lstrip('/')
    
    # Determine which addressbook we're querying
    abook_name = None
    if path and '/' not in path:
        abook_name = unquote(path)
    
    # Determine the addressbook subpath
    if abook_name == 'contacts':
        subpath = ""  # Legacy: root directory
    elif abook_name:
        subpath = abook_name
    else:
        subpath = ""  # Default to root
    
    try:
        logger.info(f"[CardDAV] REPORT request for path: {path}, abook_name: {abook_name}, subpath: {subpath}")
        root = ET.fromstring(body)
        logger.info(f"[CardDAV] Parsed REPORT body, root tag: {root.tag}")
        
        # Register namespaces for easier searching
        namespaces = {
            'D': 'DAV:',
            'C': 'urn:ietf:params:xml:ns:carddav'
        }
        
        # Check if root itself is addressbook-query or addressbook-multiget
        query_elem = None
        multiget_elem = None
        
        if root.tag.endswith('addressbook-query') or root.tag == '{urn:ietf:params:xml:ns:carddav}addressbook-query':
            query_elem = root
        else:
            # Check for addressbook-query or addressbook-multiget as children
            query_elem = root.find('.//{urn:ietf:params:xml:ns:carddav}addressbook-query')
            if query_elem is None:
                query_elem = root.find('.//C:addressbook-query', namespaces)
            if query_elem is None:
                query_elem = root.find('.//addressbook-query')
        
        if root.tag.endswith('addressbook-multiget') or root.tag == '{urn:ietf:params:xml:ns:carddav}addressbook-multiget':
            multiget_elem = root
        else:
            multiget_elem = root.find('.//{urn:ietf:params:xml:ns:carddav}addressbook-multiget')
            if multiget_elem is None:
                multiget_elem = root.find('.//C:addressbook-multiget', namespaces)
            if multiget_elem is None:
                multiget_elem = root.find('.//addressbook-multiget')
        
        logger.info(f"[CardDAV] query_elem: {query_elem is not None}, multiget_elem: {multiget_elem is not None}")
        
        items = []
        
        if query_elem is not None:
            # Addressbook query - list all contacts
            file_items = proxy.list_files(subpath)
            logger.info(f"[CardDAV] REPORT query for addressbook '{abook_name}' (subpath='{subpath}'): found {len(file_items)} items")
            
            vcf_count = 0
            processed_count = 0
            read_failed_count = 0
            added_count = 0
            
            for item in file_items:
                name = item.get('name', '')
                if name.endswith('.vcf'):
                    vcf_count += 1
                    try:
                        # Build filepath
                        if subpath:
                            filepath = f"{subpath}/{name}"
                        else:
                            filepath = name
                        
                        logger.debug(f"[CardDAV] Processing contact file {vcf_count}: {filepath}")
                        
                        # Read contact data using proxy
                        vcard_data = proxy.read_file(filepath)
                        if not vcard_data:
                            read_failed_count += 1
                            logger.warning(f"[CardDAV] Failed to read contact file: {filepath}")
                            continue
                        
                        processed_count += 1
                        contact_uid = name.replace('.vcf', '')
                        href_path = f"{abook_name}/{contact_uid}.vcf" if abook_name else f"contacts/{contact_uid}.vcf"
                        etag = str(item.get('modified', item.get('mtime', 0)))
                        items.append({
                            "href": f"{base_url}/{href_path}",
                            "props": {
                                "getcontenttype": "text/vcard; charset=utf-8",
                                "getetag": etag,
                                "address-data": vcard_data
                            }
                        })
                        added_count += 1
                        if added_count <= 5 or added_count % 50 == 0:
                            logger.info(f"[CardDAV] Added contact {added_count}: {contact_uid}")
                    except Exception as e:
                        logger.warning(f"[CardDAV] Error processing contact {name}: {e}", exc_info=True)
                        continue
            
            logger.info(f"[CardDAV] REPORT query summary for addressbook '{abook_name}':")
            logger.info(f"  - Total .vcf files: {vcf_count}")
            logger.info(f"  - Successfully read: {processed_count}")
            logger.info(f"  - Failed to read: {read_failed_count}")
            logger.info(f"  - Added to response: {added_count}")
            logger.info(f"[CardDAV] REPORT query returning {len(items)} contacts for addressbook '{abook_name}'")
        
        elif multiget_elem is not None:
            # Addressbook multiget - get specific contacts by href
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
            logger.info(f"[CardDAV] Multiget request for {len(hrefs)} hrefs")
            for href in hrefs:
                # Extract addressbook name and UID from href
                match = re.search(r'/([^/]+)/([^/]+)\.vcf$', href)
                if match:
                    href_abook_name = unquote(match.group(1))
                    contact_uid = match.group(2)
                    
                    # Build filepath
                    if href_abook_name == 'contacts':
                        filepath = f"{contact_uid}.vcf"
                    else:
                        filepath = f"{href_abook_name}/{contact_uid}.vcf"
                    
                    # Read contact using proxy
                    vcard_data = proxy.read_file(filepath)
                    if vcard_data:
                        # Get file info for etag
                        file_items = proxy.list_files(href_abook_name if href_abook_name != 'contacts' else "")
                        etag = "0"
                        for item in file_items:
                            if item.get('name') == f"{contact_uid}.vcf":
                                etag = str(item.get('modified', item.get('mtime', 0)))
                                break
                        
                        items.append({
                            "href": href,
                            "props": {
                                "getcontenttype": "text/vcard; charset=utf-8",
                                "getetag": etag,
                                "address-data": vcard_data
                            }
                        })
        
        xml = create_cardav_response(items)
        return Response(content=xml, media_type="application/xml", status_code=207)
    except Exception as e:
        logger.error(f"[CardDAV] Error handling REPORT: {e}", exc_info=True)
        return Response(content="", status_code=500)


async def handle_get(path: str, user: User, db: Session) -> Response:
    """Handle GET request (retrieve contact). Uses storage proxy if configured."""
    from urllib.parse import unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Use storage proxy (must be configured)
    proxy = DAVStorageProxy(db, user.username, 'cardav')
    
    # Extract addressbook name and contact UID from path
    match = re.search(r'/([^/]+)/([^/]+)\.vcf$', path)
    if match:
        abook_name = unquote(match.group(1))
        contact_uid = match.group(2)
        
        # Build filepath (with addressbook subdirectory if not 'contacts')
        if abook_name == 'contacts':
            filepath = f"{contact_uid}.vcf"
        else:
            filepath = f"{abook_name}/{contact_uid}.vcf"
        
        # Read file using proxy
        vcard_data = proxy.read_file(filepath)
        if vcard_data:
            return Response(content=vcard_data, media_type="text/vcard; charset=utf-8")
        else:
            logger.warning(f"Contact file not found: {filepath}")
            return Response(content="Not found", status_code=404)
    
    return Response(content="Not found", status_code=404)


async def handle_put(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle PUT request (create/update contact). Uses storage proxy if configured."""
    from urllib.parse import unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    body = await request.body()
    
    # Use storage proxy (must be configured)
    proxy = DAVStorageProxy(db, user.username, 'cardav')
    
    try:
        vcard_data = body.decode('utf-8')
        
        # Extract UID from vCard data
        contact_uid = get_contact_uid_from_vcard(vcard_data)
        
        # Extract addressbook name from path
        match = re.search(r'/([^/]+)/([^/]+)\.vcf$', path)
        if match:
            abook_name = unquote(match.group(1))
            path_uid = match.group(2)
            if contact_uid and contact_uid != path_uid:
                logger.warning(f"UID mismatch: path={path_uid}, vcard={contact_uid}, using path UID")
            contact_uid = path_uid
        else:
            # Legacy path or no addressbook specified
            abook_name = 'contacts'
        
        if not contact_uid:
            # Generate UID if not present
            contact_uid = str(uuid.uuid4())
            # Add UID to vCard data
            vcard = vobject.readOne(vcard_data)
            if not hasattr(vcard, 'uid'):
                vcard.add('uid')
            vcard.uid.value = contact_uid
            vcard_data = vcard.serialize()
        
        # Build filepath (with addressbook subdirectory if not 'contacts')
        if abook_name == 'contacts':
            filepath = f"{contact_uid}.vcf"
        else:
            filepath = f"{abook_name}/{contact_uid}.vcf"
        
        # Save to file using proxy
        success = proxy.write_file(filepath, vcard_data)
        
        if success:
            logger.info(f"Saved contact {contact_uid} for user {user.username} in addressbook {abook_name}")
            return Response(content="", status_code=201)
        else:
            logger.error(f"Failed to save contact {contact_uid}")
            return Response(content="Error saving contact", status_code=500)
    except Exception as e:
        logger.error(f"Error saving contact: {e}")
        return Response(content=f"Error: {e}", status_code=500)


async def handle_delete(path: str, user: User, db: Session) -> Response:
    """Handle DELETE request. Uses storage proxy if configured."""
    from urllib.parse import unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Use storage proxy (must be configured)
    proxy = DAVStorageProxy(db, user.username, 'cardav')
    
    # Extract addressbook name and contact UID from path
    match = re.search(r'/([^/]+)/([^/]+)\.vcf$', path)
    if match:
        abook_name = unquote(match.group(1))
        contact_uid = match.group(2)
        
        # Build filepath (with addressbook subdirectory if not 'contacts')
        if abook_name == 'contacts':
            filepath = f"{contact_uid}.vcf"
        else:
            filepath = f"{abook_name}/{contact_uid}.vcf"
        
        # Delete file using proxy
        success = proxy.delete_file(filepath)
        if success:
            logger.info(f"Deleted contact {contact_uid} from addressbook {abook_name} for user {user.username}")
            return Response(content="", status_code=204)
        else:
            logger.warning(f"Contact file not found or failed to delete: {filepath}")
            return Response(content="Not found", status_code=404)
    
    return Response(content="Not found", status_code=404)


async def handle_mkcol(path: str, user: User, db: Session) -> Response:
    """Handle MKCOL request. Uses storage proxy if configured."""
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Use storage proxy (must be configured)
    proxy = DAVStorageProxy(db, user.username, 'cardav')
    
    # Create addressbook by writing a placeholder file
    # The directory will be created automatically when we write a file
    placeholder_path = f"{path}/.cardav_placeholder" if path else ".cardav_placeholder"
    if proxy.write_file(placeholder_path, "# CardDAV Addressbook Directory"):
        return Response(content="", status_code=201)
    else:
        return Response(content="Failed to create addressbook", status_code=500)


async def handle_proppatch(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle PROPPATCH request (set addressbook properties)."""
    body = await request.body()
    
    try:
        # Parse the PROPPATCH request
        root = ET.fromstring(body)
        
        # For now, accept changes without storing them
        # Properties are hardcoded in handle_propfind
        
        xml = '''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
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


def create_cardav_app() -> FastAPI:
    """Create CardDAV FastAPI application."""
    app = FastAPI(title="Posterchanai CardDAV Server")
    
    @app.route("/.well-known/carddav", methods=["GET", "HEAD", "OPTIONS", "PROPFIND"])
    async def carddav_discovery(request: StarletteRequest):
        """CardDAV discovery endpoint. Returns 302 redirect to CardDAV principal or handles PROPFIND."""
        # Handle PROPFIND requests (some clients like iPhone use this)
        if request.method == "PROPFIND":
            # Get DB session for authentication
            db = SessionLocal()
            try:
                # Extract username from Basic Auth
                auth_header = request.headers.get("Authorization", "")
                if not auth_header.startswith("Basic "):
                    return Response(
                        content="Unauthorized",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                    )
                
                # Parse Basic Auth
                try:
                    auth_data = auth_header[6:]
                    decoded = base64.b64decode(auth_data).decode('utf-8')
                    if ':' not in decoded:
                        return Response(
                            content="Invalid credentials",
                            status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                        )
                    username, password = decoded.split(':', 1)
                    username = username.strip()
                    password = password.strip()
                except Exception:
                    return Response(
                        content="Invalid credentials",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                    )
                
                # Verify user
                user = db.query(User).filter(User.username == username).first()
                if not user and '@' in username:
                    username_part = username.split('@')[0]
                    user = db.query(User).filter(User.username.like(f"{username_part}@%")).first()
                
                if not user:
                    return Response(
                        content="Invalid credentials",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                    )
                
                # Verify password
                if not verify_password(password, user.password_hash):
                    return Response(
                        content="Invalid credentials",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                    )
                
                # Return principal URL in PROPFIND response
                from urllib.parse import quote
                encoded_username = quote(user.username, safe='')
                principal_url = f"/carddav/{encoded_username}/"
                
                xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
    <D:response>
        <D:href>/.well-known/carddav</D:href>
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
        # Get the host from the request
        host = request.headers.get("Host", "ai.poster.place")
        # Determine if we're using HTTPS (check X-Forwarded-Proto or assume HTTPS if port 443)
        scheme = request.headers.get("X-Forwarded-Proto", "https")
        if not scheme or scheme == "http":
            # Check if we're behind a proxy that terminates SSL
            if request.url.scheme == "https" or "443" in str(request.url.port):
                scheme = "https"
        
        # Use absolute URL for redirect (required by some clients like iPhone)
        redirect_url = f"{scheme}://{host}/carddav/"
        
        return Response(
            content="",
            status_code=302,  # Use 302 (Found) instead of 301 (Moved Permanently) for better compatibility
            headers={
                "Location": redirect_url,
                "Cache-Control": "no-cache"
            }
        )
    
    @app.api_route("/principals/", methods=["GET", "POST", "PUT", "DELETE", "PROPFIND", "PROPPATCH", "REPORT", "MKCOL", "OPTIONS"])
    async def principals_root_handler(request: StarletteRequest):
        """Handle DAV principals root requests (/principals/)."""
        return await principals_handler(request, "")
    
    @app.api_route("/principals/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PROPFIND", "PROPPATCH", "REPORT", "MKCOL", "OPTIONS"])
    async def principals_handler(request: StarletteRequest, path: str = ""):
        """Handle DAV principals requests. Used by iPhone for principal discovery."""
        # Get path parameter
        if not path:
            path = request.path_params.get("path", "")
        
        # Handle OPTIONS without auth
        if request.method == "OPTIONS":
            return Response(
                content="",
                status_code=200,
                headers={
                    "DAV": "1, 2, 3, addressbook",
                    "Allow": "OPTIONS, GET, HEAD, POST, PUT, DELETE, PROPFIND, PROPPATCH, REPORT, MKCOL",
                }
            )
        
        # Get DB session
        db = SessionLocal()
        try:
            # Extract username from Basic Auth
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Basic "):
                return Response(
                    content="Unauthorized",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                )
            
            # Parse Basic Auth
            try:
                auth_data = auth_header[6:]
                decoded = base64.b64decode(auth_data).decode('utf-8')
                if ':' not in decoded:
                    return Response(
                        content="Invalid credentials",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                    )
                username, password = decoded.split(':', 1)
                username = username.strip()
                password = password.strip()
            except Exception:
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                )
            
            # Verify user
            user = db.query(User).filter(User.username == username).first()
            if not user and '@' in username:
                username_part = username.split('@')[0]
                user = db.query(User).filter(User.username.like(f"{username_part}@%")).first()
            
            if not user:
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                )
            
            # Verify password
            if not verify_password(password, user.password_hash):
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                )
            
            # Handle PROPFIND for principals
            if request.method == "PROPFIND":
                from urllib.parse import quote, unquote
                encoded_username = quote(user.username, safe='')
                carddav_principal = f"/carddav/{encoded_username}/"
                
                # Normalize path
                path = path.rstrip('/')
                if path:
                    # Extract username from path (e.g., /principals/verita84%40poster.place/)
                    path_username = unquote(path)
                    if path_username == user.username or path_username == encoded_username:
                        # Return principal info
                        xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
    <D:response>
        <D:href>/principals/{encoded_username}/</D:href>
        <D:propstat>
            <D:prop>
                <D:resourcetype><D:collection/></D:resourcetype>
                <D:displayname>{user.username}</D:displayname>
                <C:addressbook-home-set xmlns:C="urn:ietf:params:xml:ns:carddav">
                    <D:href>{carddav_principal}</D:href>
                </C:addressbook-home-set>
            </D:prop>
            <D:status>HTTP/1.1 200 OK</D:status>
        </D:propstat>
    </D:response>
</D:multistatus>'''
                        return Response(content=xml, media_type="application/xml", status_code=207)
                
                # List all principals (root /principals/)
                xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
    <D:response>
        <D:href>/principals/{encoded_username}/</D:href>
        <D:propstat>
            <D:prop>
                <D:resourcetype><D:collection/></D:resourcetype>
                <D:displayname>{user.username}</D:displayname>
            </D:prop>
            <D:status>HTTP/1.1 200 OK</D:status>
        </D:propstat>
    </D:response>
</D:multistatus>'''
                return Response(content=xml, media_type="application/xml", status_code=207)
            
            # For other methods, return 405
            return Response(content="Method not allowed", status_code=405)
        finally:
            db.close()
    
    @app.api_route("/carddav/", methods=["GET", "POST", "PUT", "DELETE", "PROPFIND", "PROPPATCH", "REPORT", "MKCOL", "OPTIONS"])
    async def carddav_root_handler(request: StarletteRequest):
        """Handle CardDAV root requests (/carddav/)."""
        # Delegate to the main handler with empty path
        return await carddav_handler(request)
    
    @app.api_route("/carddav/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PROPFIND", "PROPPATCH", "REPORT", "MKCOL", "OPTIONS"])
    async def carddav_handler(request: StarletteRequest):
        """Handle CardDAV requests."""
        # Get path parameter
        path = request.path_params.get("path", "")
        
        # Handle OPTIONS without auth (for discovery)
        if request.method == "OPTIONS":
            return Response(
                content="",
                status_code=200,
                headers={
                    "DAV": "1, 2, 3, addressbook",
                    "Allow": "OPTIONS, GET, HEAD, POST, PUT, DELETE, PROPFIND, PROPPATCH, REPORT, MKCOL",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "OPTIONS, GET, HEAD, POST, PUT, DELETE, PROPFIND, PROPPATCH, REPORT, MKCOL",
                    "Access-Control-Allow-Headers": "Content-Type, Depth, User-Agent, X-Requested-With, If-None-Match, Authorization"
                }
            )
        
        # Get DB session manually (can't use Depends with @app.route)
        db = SessionLocal()
        try:
            # Extract username from Basic Auth
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Basic "):
                logger.warning(f"[CardDAV] No Basic auth header: {auth_header[:20] if auth_header else 'empty'}")
                return Response(
                    content="Unauthorized",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                )
            
            # Parse Basic Auth
            try:
                auth_data = auth_header[6:]  # Remove "Basic "
                decoded = base64.b64decode(auth_data).decode('utf-8')
                if ':' not in decoded:
                    logger.warning(f"[CardDAV] No colon in decoded credentials")
                    return Response(
                        content="Invalid credentials",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                    )
                username, password = decoded.split(':', 1)
                # Strip any whitespace that might have been introduced
                username = username.strip()
                password = password.strip()
                logger.info(f"[CardDAV] Auth attempt - username: '{username}' (len={len(username)}), password len: {len(password)}")
            except Exception as e:
                logger.warning(f"[CardDAV] Failed to parse credentials: {e}", exc_info=True)
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                )
            
            # Verify user - try exact match first, then try without domain
            user = db.query(User).filter(User.username == username).first()
            if not user and '@' in username:
                # Try without domain (some clients send just the username part)
                username_part = username.split('@')[0]
                user = db.query(User).filter(User.username.like(f"{username_part}@%")).first()
                if user:
                    logger.info(f"[CardDAV] Matched user by username part: {username} -> {user.username}")
            
            if not user:
                logger.warning(f"[CardDAV] User not found: {username}")
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                )
            
            # Verify password
            try:
                password_valid = verify_password(password, user.password_hash)
                if not password_valid:
                    # Try with password without stripping (in case whitespace is significant)
                    password_valid_alt = verify_password(password.strip(), user.password_hash) if password != password.strip() else False
                    if not password_valid_alt:
                        logger.warning(f"[CardDAV] Invalid password for user: {user.username} (auth username: '{username}', password len: {len(password)}, hash: {user.password_hash[:20]}...)")
                        return Response(
                            content="Invalid credentials",
                            status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                        )
                    else:
                        logger.info(f"[CardDAV] Password verified after stripping whitespace")
            except Exception as e:
                logger.error(f"[CardDAV] Error verifying password: {e}", exc_info=True)
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                )
            
            logger.info(f"[CardDAV] Successfully authenticated user: {user.username}")
            
            # Get depth header for PROPFIND
            depth = request.headers.get("Depth", "0")
            
            # Handle CardDAV methods
            method = request.method
            
            if method == "PROPFIND":
                return await handle_propfind(path, user, db, request, depth)
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
            elif method == "MKCOL":
                return await handle_mkcol(path, user, db)
            else:
                return Response(content="Method not allowed", status_code=405)
        finally:
            db.close()
    
    return app


def start_cardav_server(db: Session, port: int = 8082) -> bool:
    """Start the CardDAV server in a background thread."""
    global _cardav_app, _cardav_server, _cardav_thread
    
    if _cardav_server is not None:
        logger.warning("CardDAV server already running")
        return False
    
    try:
        _cardav_app = create_cardav_app()
        
        # Check for SSL certificate settings
        from app.models import Setting
        ssl_cert_setting = db.query(Setting).filter(Setting.key == "cardav_ssl_cert").first()
        ssl_key_setting = db.query(Setting).filter(Setting.key == "cardav_ssl_key").first()
        
        ssl_keyfile = ssl_key_setting.value if ssl_key_setting and ssl_key_setting.value else None
        ssl_certfile = ssl_cert_setting.value if ssl_cert_setting and ssl_cert_setting.value else None
        
        config_kwargs = {
            "app": _cardav_app,
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
                logger.info(f"[CardDAV] SSL enabled: cert={ssl_certfile}, key={ssl_keyfile}")
            else:
                logger.warning(f"[CardDAV] SSL certificates not found, starting without SSL")
        
        config = uvicorn.Config(**config_kwargs)
        _cardav_server = uvicorn.Server(config)
        
        def run_server():
            try:
                logger.info(f"[CardDAV] Starting server on port {port}")
                _cardav_server.run()
            except Exception as e:
                logger.error(f"[CardDAV] Server error: {e}", exc_info=True)
        
        _cardav_thread = threading.Thread(target=run_server, daemon=True)
        _cardav_thread.start()
        
        logger.info(f"[CardDAV] Server started on port {port}")
        return True
    except Exception as e:
        logger.error(f"[CardDAV] Failed to start server: {e}", exc_info=True)
        return False


def stop_cardav_server():
    """Stop the CardDAV server."""
    global _cardav_server, _cardav_thread
    
    if _cardav_server is None:
        return
    
    try:
        _cardav_server.should_exit = True
        _cardav_server = None
        if _cardav_thread:
            _cardav_thread.join(timeout=5)
            _cardav_thread = None
        logger.info("[CardDAV] Server stopped")
    except Exception as e:
        logger.error(f"[CardDAV] Error stopping server: {e}", exc_info=True)


def is_cardav_running() -> bool:
    """Check if CardDAV server is running."""
    return _cardav_server is not None
