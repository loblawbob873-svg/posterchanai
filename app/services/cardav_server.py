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


async def handle_propfind(path: str, user: User, db: Session, depth: str = "0") -> Response:
    """Handle PROPFIND request. Uses storage proxy if configured."""
    from urllib.parse import quote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Use storage proxy (will fallback to local if not configured)
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
    if not path or path == '':
        items.append({
            "href": f"{base_url}/",
            "props": {
                "resourcetype": "collection",
                "displayname": f"{user.username}'s Addressbooks"
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
                items.append({
                    "href": f"{base_url}/contacts/",
                    "props": {
                        "resourcetype": "addressbook",
                        "displayname": "Contacts"
                    }
                })
            else:
                # New mode: show actual addressbook subdirectories
                for abook_name in sorted(addressbook_dirs):
                    items.append({
                        "href": f"{base_url}/{quote(abook_name, safe='')}/",
                        "props": {
                            "resourcetype": "addressbook",
                            "displayname": abook_name.replace('_', ' ').title()
                        }
                    })
    
    # Individual addressbook
    elif '/' not in path:
        abook_name = path
        subpath = "" if abook_name == 'contacts' else abook_name
        
        items.append({
            "href": f"{base_url}/{quote(abook_name, safe='')}/",
            "props": {
                "resourcetype": "addressbook",
                "displayname": abook_name.replace('_', ' ').title() if abook_name != 'contacts' else "Contacts"
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
    
    # Use storage proxy (will fallback to local if not configured)
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
        root = ET.fromstring(body)
        # Check for addressbook-query or addressbook-multiget
        query_elem = root.find('.//{urn:ietf:params:xml:ns:carddav}addressbook-query')
        multiget_elem = root.find('.//{urn:ietf:params:xml:ns:carddav}addressbook-multiget')
        
        items = []
        
        if query_elem is not None:
            # Addressbook query - list all contacts
            file_items = proxy.list_files(subpath)
            for item in file_items:
                name = item.get('name', '')
                if name.endswith('.vcf'):
                    try:
                        # Build filepath
                        if subpath:
                            filepath = f"{subpath}/{name}"
                        else:
                            filepath = name
                        
                        # Read contact data using proxy
                        vcard_data = proxy.read_file(filepath)
                        if vcard_data:
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
                    except Exception as e:
                        logger.debug(f"Error processing {name}: {e}")
                        continue
        
        elif multiget_elem is not None:
            # Addressbook multiget - get specific contacts by href
            hrefs = [elem.text for elem in multiget_elem.findall('.//{DAV:}href')]
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
        logger.error(f"Error handling REPORT: {e}")
        return Response(content="", status_code=500)


async def handle_get(path: str, user: User, db: Session) -> Response:
    """Handle GET request (retrieve contact). Uses storage proxy if configured."""
    from urllib.parse import unquote
    from app.services.dav_storage_proxy import DAVStorageProxy
    
    # Use storage proxy (will fallback to local if not configured)
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
    
    # Use storage proxy (will fallback to local if not configured)
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
    
    # Use storage proxy (will fallback to local if not configured)
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
    """Handle MKCOL request."""
    # Addressbook already exists (created on first access)
    cardav_path = get_user_cardav_path(user, db)
    cardav_path.mkdir(parents=True, exist_ok=True)
    return Response(content="", status_code=201)


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
    
    @app.route("/.well-known/carddav", methods=["GET", "HEAD", "OPTIONS"])
    async def carddav_discovery(request: StarletteRequest):
        """CardDAV discovery endpoint."""
        return Response(
            content="",
            status_code=301,
            headers={"Location": "/carddav/"}
        )
    
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
                credentials = base64.b64decode(auth_header[6:]).decode('utf-8')
                username, password = credentials.split(':', 1)
                logger.debug(f"[CardDAV] Parsed credentials - username: {username}, password length: {len(password)}")
            except Exception as e:
                logger.warning(f"[CardDAV] Failed to parse credentials: {e}")
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
                    logger.debug(f"[CardDAV] Matched user by username part: {username} -> {user.username}")
            
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
                    logger.warning(f"[CardDAV] Invalid password for user: {user.username} (auth username: {username}, password chars: {len(password)})")
                    return Response(
                        content="Invalid credentials",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                    )
            except Exception as e:
                logger.error(f"[CardDAV] Error verifying password: {e}")
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Posterchanai CardDAV"'}
                )
            
            logger.debug(f"[CardDAV] Authenticated user: {user.username}")
            
            # Get depth header for PROPFIND
            depth = request.headers.get("Depth", "0")
            
            # Handle CardDAV methods
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
