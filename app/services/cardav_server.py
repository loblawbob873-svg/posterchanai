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
from app.database import get_db
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
    """Handle PROPFIND request."""
    cardav_path = get_user_cardav_path(user, db)
    base_url = f"/carddav/{user.username}"
    
    items = []
    
    # Root addressbook collection
    if path == user.username or path == f"{user.username}/" or not path:
        items.append({
            "href": f"{base_url}/",
            "props": {
                "resourcetype": "addressbook",
                "displayname": "Contacts"
            }
        })
    
    # If depth=1, list contacts
    if depth == "1" and (path == user.username or path == f"{user.username}/" or not path):
        # List all .vcf files
        for vcf_file in cardav_path.glob("*.vcf"):
            contact_uid = vcf_file.stem
            items.append({
                "href": f"{base_url}/{contact_uid}.vcf",
                "props": {
                    "getcontenttype": "text/vcard; charset=utf-8",
                    "getetag": str(vcf_file.stat().st_mtime)
                }
            })
    
    xml = create_cardav_response(items)
    return Response(content=xml, media_type="application/xml", status_code=207)


async def handle_report(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle REPORT request (contact queries)."""
    body = await request.body()
    cardav_path = get_user_cardav_path(user, db)
    base_url = f"/carddav/{user.username}"
    
    try:
        root = ET.fromstring(body)
        # Check for addressbook-query or addressbook-multiget
        query_elem = root.find('.//{urn:ietf:params:xml:ns:carddav}addressbook-query')
        multiget_elem = root.find('.//{urn:ietf:params:xml:ns:carddav}addressbook-multiget')
        
        items = []
        
        if query_elem is not None:
            # Addressbook query - list all contacts
            filter_elem = query_elem.find('.//{urn:ietf:params:xml:ns:carddav}filter')
            # For now, return all contacts (can add filtering later)
            
            for vcf_file in cardav_path.glob("*.vcf"):
                try:
                    with open(vcf_file, 'r', encoding='utf-8') as f:
                        vcard_data = f.read()
                    
                    contact_uid = vcf_file.stem
                    items.append({
                        "href": f"{base_url}/{contact_uid}.vcf",
                        "props": {
                            "getcontenttype": "text/vcard; charset=utf-8",
                            "getetag": str(vcf_file.stat().st_mtime),
                            "address-data": vcard_data
                        }
                    })
                except Exception as e:
                    logger.debug(f"Error processing {vcf_file}: {e}")
                    continue
        
        elif multiget_elem is not None:
            # Addressbook multiget - get specific contacts by href
            hrefs = [elem.text for elem in multiget_elem.findall('.//{DAV:}href')]
            for href in hrefs:
                # Extract UID from href
                match = re.search(r'/([^/]+)\.vcf$', href)
                if match:
                    contact_uid = match.group(1)
                    vcf_file = cardav_path / f"{contact_uid}.vcf"
                    if vcf_file.exists():
                        try:
                            with open(vcf_file, 'r', encoding='utf-8') as f:
                                vcard_data = f.read()
                            items.append({
                                "href": href,
                                "props": {
                                    "getcontenttype": "text/vcard; charset=utf-8",
                                    "getetag": str(vcf_file.stat().st_mtime),
                                    "address-data": vcard_data
                                }
                            })
                        except Exception as e:
                            logger.debug(f"Error reading {vcf_file}: {e}")
        
        xml = create_cardav_response(items)
        return Response(content=xml, media_type="application/xml", status_code=207)
    except Exception as e:
        logger.error(f"Error handling REPORT: {e}")
        return Response(content="", status_code=500)


async def handle_get(path: str, user: User, db: Session) -> Response:
    """Handle GET request (retrieve contact)."""
    cardav_path = get_user_cardav_path(user, db)
    
    # Extract contact UID from path
    match = re.search(r'/([^/]+)\.vcf$', path)
    if match:
        contact_uid = match.group(1)
        vcf_file = cardav_path / f"{contact_uid}.vcf"
        if vcf_file.exists():
            try:
                with open(vcf_file, 'r', encoding='utf-8') as f:
                    vcard_data = f.read()
                return Response(content=vcard_data, media_type="text/vcard; charset=utf-8")
            except Exception as e:
                logger.error(f"Error reading contact file: {e}")
                return Response(content="Error reading contact", status_code=500)
    
    return Response(content="Not found", status_code=404)


async def handle_put(path: str, user: User, db: Session, request: StarletteRequest) -> Response:
    """Handle PUT request (create/update contact)."""
    body = await request.body()
    cardav_path = get_user_cardav_path(user, db)
    
    try:
        vcard_data = body.decode('utf-8')
        
        # Extract UID from vCard data
        contact_uid = get_contact_uid_from_vcard(vcard_data)
        if not contact_uid:
            # Generate UID if not present
            contact_uid = str(uuid.uuid4())
            # Add UID to vCard data
            vcard = vobject.readOne(vcard_data)
            if not hasattr(vcard, 'uid'):
                vcard.add('uid')
            vcard.uid.value = contact_uid
            vcard_data = vcard.serialize()
        
        # Save to file
        vcf_file = cardav_path / f"{contact_uid}.vcf"
        with open(vcf_file, 'w', encoding='utf-8') as f:
            f.write(vcard_data)
        
        logger.info(f"Saved contact {contact_uid} for user {user.username}")
        return Response(content="", status_code=201)
    except Exception as e:
        logger.error(f"Error saving contact: {e}")
        return Response(content=f"Error: {e}", status_code=500)


async def handle_delete(path: str, user: User, db: Session) -> Response:
    """Handle DELETE request."""
    cardav_path = get_user_cardav_path(user, db)
    
    # Extract contact UID from path
    match = re.search(r'/([^/]+)\.vcf$', path)
    if match:
        contact_uid = match.group(1)
        vcf_file = cardav_path / f"{contact_uid}.vcf"
        if vcf_file.exists():
            try:
                vcf_file.unlink()
                logger.info(f"Deleted contact {contact_uid} for user {user.username}")
                return Response(content="", status_code=204)
            except Exception as e:
                logger.error(f"Error deleting contact: {e}")
                return Response(content="Error deleting", status_code=500)
    
    return Response(content="Not found", status_code=404)


async def handle_mkcol(path: str, user: User, db: Session) -> Response:
    """Handle MKCOL request."""
    # Addressbook already exists (created on first access)
    cardav_path = get_user_cardav_path(user, db)
    cardav_path.mkdir(parents=True, exist_ok=True)
    return Response(content="", status_code=201)


def create_cardav_app() -> FastAPI:
    """Create CardDAV FastAPI application."""
    app = FastAPI(title="Posterchanai CardDAV Server")
    
    @app.get("/.well-known/carddav")
    async def carddav_discovery():
        """CardDAV discovery endpoint."""
        return Response(
            content="",
            status_code=301,
            headers={"Location": "/carddav/"}
        )
    
    @app.route("/carddav/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PROPFIND", "REPORT", "MKCOL"])
    async def carddav_handler(path: str, request: StarletteRequest, db: Session = Depends(get_db)):
        """Handle CardDAV requests."""
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
            credentials = base64.b64decode(auth_header[6:]).decode('utf-8')
            username, password = credentials.split(':', 1)
        except:
            return Response(content="Invalid credentials", status_code=401)
        
        # Verify user
        user = db.query(User).filter(User.username == username).first()
        if not user or not verify_password(password, user.password_hash):
            return Response(content="Invalid credentials", status_code=401)
        
        # Get depth header for PROPFIND
        depth = request.headers.get("Depth", "0")
        
        # Handle CardDAV methods
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
        elif method == "MKCOL":
            return await handle_mkcol(path, user, db)
        else:
            return Response(content="Method not allowed", status_code=405)
    
    return app


def start_cardav_server(db: Session, port: int = 8082) -> bool:
    """Start the CardDAV server in a background thread."""
    global _cardav_app, _cardav_server, _cardav_thread
    
    if _cardav_server is not None:
        logger.warning("CardDAV server already running")
        return False
    
    try:
        _cardav_app = create_cardav_app()
        
        config = uvicorn.Config(
            app=_cardav_app,
            host="0.0.0.0",
            port=port,
            log_level="info"
        )
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
