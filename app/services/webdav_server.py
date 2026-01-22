"""
Built-in WebDAV Server - Serves user files via WebDAV protocol.
Uses the storage configuration and respects storage quotas.
"""
import logging
import threading
import os
import time
from pathlib import Path
from typing import Optional, Dict, Tuple
from sqlalchemy.orm import Session
from wsgidav.wsgidav_app import WsgiDAVApp
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.dav_provider import DAVProvider, DAVCollection, DAVNonCollection, _DAVResource
from cheroot.wsgi import Server as WSGIServer

from app.models import User, Setting

# Initialize logger first, before monkey patches that use it
logger = logging.getLogger(__name__)

# Register DAV namespace with 'd' prefix (lowercase) for Joplin compatibility
# Joplin's XML parser expects lowercase 'd:' namespace prefix
try:
    from xml.etree import ElementTree as ET
    ET.register_namespace('d', 'DAV:')
    # Also register default namespace
    ET.register_namespace('', 'DAV:')
except Exception:
    pass

# Also try to register with wsgidav's etree if it uses lxml
try:
    from wsgidav.util import etree
    if hasattr(etree, 'register_namespace'):
        etree.register_namespace('d', 'DAV:')
        etree.register_namespace('', 'DAV:')
except Exception:
    pass

# Monkey-patch _DAVResource to ensure path is always a string
_original_dav_resource_init = _DAVResource.__init__

def _patched_dav_resource_init(self, path, is_collection, environ):
    """Patched __init__ that ensures path is always a string."""
    # Convert list to string if needed
    if isinstance(path, list):
        logger.error(f"[WebDAV] _DAVResource.__init__ received path as list: {path}, converting to string")
        path = str(path[0]) if len(path) > 0 else ''
    # Ensure it's a string
    path = str(path)
    # Call original init
    _original_dav_resource_init(self, path, is_collection, environ)

# Apply the monkey patch
_DAVResource.__init__ = _patched_dav_resource_init

# Also monkey-patch get_href to ensure it always returns a string
_original_get_href = _DAVResource.get_href if hasattr(_DAVResource, 'get_href') else None

def _patched_get_href(self):
    """Patched get_href that ensures return value is always a string. Handles SCRIPT_NAME prefix correctly."""
    # First, check if this class has its own get_href implementation (not inherited)
    # If so, call it directly to avoid infinite recursion
    if type(self).get_href != _DAVResource.get_href if hasattr(_DAVResource, 'get_href') else None:
        # This class has overridden get_href, so call it
        method = type(self).get_href
        if method and method != _patched_get_href:
            try:
                result = method(self)
                # Ensure result is a string
                if isinstance(result, list):
                    result = str(result[0]) if len(result) > 0 else ''
                result = str(result)
                
                # CRITICAL: If the custom get_href already includes /webdav/, we need to prevent WSGiDAV from prepending it again
                # WSGiDAV will prepend SCRIPT_NAME, so if href already has /webdav/, we should return it as-is
                # But WSGiDAV might still prepend, so we need to check the environ
                script_name = self.environ.get('SCRIPT_NAME', '') if hasattr(self, 'environ') and self.environ else ''
                if script_name and result.startswith(script_name):
                    # Href already includes script_name, but WSGiDAV will prepend it again
                    # Return the href without script_name so WSGiDAV can prepend it correctly
                    # OR: return as-is and let WSGiDAV handle it (it might detect and not double-prefix)
                    # Actually, let's return it as-is - WSGiDAV should handle this correctly
                    pass
                
                return result
            except Exception as e:
                logger.error(f"[WebDAV] Error calling custom get_href: {e}")
    
    # Fallback: use path directly
    if hasattr(self, 'path'):
        path_value = self.path
        if isinstance(path_value, list):
            logger.debug(f"[WebDAV]  Resource.path is a list in get_href: {path_value}, converting to string")
            result = str(path_value[0]) if len(path_value) > 0 else ''
        else:
            result = str(path_value)
        
        return result
    
    # Final fallback
    return "/"

# Apply the patch
_DAVResource.get_href = _patched_get_href

# Also monkey-patch __setattr__ to prevent path from being set as a list
_original_setattr = _DAVResource.__setattr__

def _patched_setattr(self, name, value):
    """Patched __setattr__ that ensures path is never set as a list."""
    if name == 'path' and isinstance(value, list):
        logger.error(f"[WebDAV] Attempting to set path as list: {value}, converting to string")
        value = str(value[0]) if len(value) > 0 else ''
    _original_setattr(self, name, value)

_DAVResource.__setattr__ = _patched_setattr

# Monkey-patch add_property_response to log href values
from wsgidav import util as wsgidav_util
_original_add_property_response = wsgidav_util.add_property_response

def _patched_add_property_response(multistatus_elem, href, prop_list):
    """Patched add_property_response that ensures href is always a string and removes duplicates."""
    # Convert href to string if it's a list
    if isinstance(href, list):
        logger.error(f"[WebDAV] ⚠️  CRITICAL: href was list: {href}, using first element")
        href = str(href[0]) if len(href) > 0 else '/'

    # Ensure href is a string and not a string representation of a list
    href = str(href)

    # Extra check: if href looks like a string representation of a list, extract the actual path
    if href.startswith('[') and href.endswith(']'):
        logger.error(f"[WebDAV] ⚠️  CRITICAL: href is string representation of list: {href}")
        try:
            import ast
            href_list = ast.literal_eval(href)
            if isinstance(href_list, list) and len(href_list) > 0:
                href = str(href_list[0])
                logger.error(f"[WebDAV] Extracted href from string list: {href}")
        except Exception as e:
            logger.error(f"[WebDAV] Failed to parse href as list: {e}")
            # Fallback: strip brackets manually
            href = href.strip('[]').strip("'\"")

    # CRITICAL FIX: Check if this href already exists in any response element
    # to prevent duplicate <d:href> elements which cause JSON parsers to create arrays
    from wsgidav.util import etree
    
    # Normalize href for comparison (remove trailing slashes, ensure consistent format)
    normalized_href = str(href).rstrip('/')
    if not normalized_href.startswith('/'):
        normalized_href = '/' + normalized_href
    
    # Check all existing responses for duplicate hrefs
    for response_elem in multistatus_elem.findall("{DAV:}response"):
        existing_href_elem = response_elem.find("{DAV:}href")
        if existing_href_elem is not None and existing_href_elem.text:
            existing_href = str(existing_href_elem.text).rstrip('/')
            if not existing_href.startswith('/'):
                existing_href = '/' + existing_href
            if existing_href == normalized_href:
                logger.warning(f"[WebDAV] Duplicate response detected for href: {href} - skipping creation")
                return response_elem  # Return existing response instead of creating duplicate

    # Call original but then check if it accidentally added duplicate href elements
    result = _original_add_property_response(multistatus_elem, href, prop_list)

    # CRITICAL: After adding, check if the created response has multiple href elements
    if result is not None:
        href_elements = result.findall("{DAV:}href")
        if len(href_elements) > 1:
            logger.error(f"[WebDAV] ⚠️  CRITICAL: Response has {len(href_elements)} href elements! Removing duplicates.")
            # Keep only the first href element
            for href_elem in href_elements[1:]:
                result.remove(href_elem)
        
        # Also check if this response was added multiple times to the multistatus
        # (same href in different response elements)
        all_responses = multistatus_elem.findall("{DAV:}response")
        href_count = 0
        for resp in all_responses:
            resp_href_elem = resp.find("{DAV:}href")
            if resp_href_elem is not None and resp_href_elem.text:
                resp_href = str(resp_href_elem.text).rstrip('/')
                if not resp_href.startswith('/'):
                    resp_href = '/' + resp_href
                if resp_href == normalized_href:
                    href_count += 1
        
        if href_count > 1:
            logger.error(f"[WebDAV] ⚠️  CRITICAL: Found {href_count} response elements with same href: {href} - removing duplicates")
            # Remove all but the first occurrence
            removed = 0
            for resp in all_responses:
                if removed >= href_count - 1:
                    break
                resp_href_elem = resp.find("{DAV:}href")
                if resp_href_elem is not None and resp_href_elem.text:
                    resp_href = str(resp_href_elem.text).rstrip('/')
                    if not resp_href.startswith('/'):
                        resp_href = '/' + resp_href
                    if resp_href == normalized_href:
                        multistatus_elem.remove(resp)
                        removed += 1

    return result

wsgidav_util.add_property_response = _patched_add_property_response

# Patch send_multi_status_response to log the actual XML being sent
from wsgidav import util as wsgidav_util2
_original_send_multi_status_response = wsgidav_util2.send_multi_status_response

def _patched_send_multi_status_response(environ, start_response, multistatus_elem):
    """Log the actual XML being sent to debug Joplin issues and fix hrefs for Flacbox."""
    from wsgidav.util import etree
    
    # Count total responses in multistatus (convert to list once for reuse)
    all_responses = list(multistatus_elem.findall("{DAV:}response"))
    total_responses = len(all_responses)
    if total_responses > 100:
        logger.debug(f"[WebDAV] 📊 Multistatus XML contains {total_responses} response elements")
    else:
        logger.info(f"[WebDAV] 📊 Multistatus XML contains {total_responses} response elements")
    
    # Ensure multistatus root has proper namespace declaration
    # Some clients (like Flacbox) may require explicit xmlns:D declaration
    if multistatus_elem.tag == "{DAV:}multistatus":
        # Check if xmlns:D is declared
        nsmap = multistatus_elem.nsmap if hasattr(multistatus_elem, 'nsmap') else {}
        if 'D' not in nsmap or nsmap.get('D') != 'DAV:':
            # Set namespace map to ensure D: prefix is used
            multistatus_elem.set('xmlns:D', 'DAV:')
    
    # CRITICAL FIX: WSGiDAV strips /webdav/ from hrefs before putting them in XML
    # We must add it back so Flacbox can construct correct GET URLs
    # Also ensure all property elements are properly namespaced for Flacbox compatibility
    script_name = environ.get('SCRIPT_NAME', '/webdav')
    if script_name:
        hrefs_fixed = 0
        props_fixed = 0
        # all_responses and total_responses already calculated above
        
        # For very large responses, optimize by skipping expensive operations
        # Most hrefs should already be correct from get_href(), so we only fix ones that need it
        is_large_response = total_responses > 1000
        
        for response in all_responses:
            href_elem = response.find("{DAV:}href")
            if href_elem is not None and href_elem.text:
                href_text = href_elem.text
                import re
                original_href = href_text
                
                # CRITICAL: Ensure hrefs are absolute with /webdav/username@domain/ prefix for Flacbox
                # Flacbox needs absolute hrefs to construct GET requests properly
                # WSGiDAV strips /webdav/ prefix, so we must reconstruct it
                needs_fixing = False
                
                # Extract username from REQUEST_URI to reconstruct full absolute path
                request_uri = environ.get('REQUEST_URI', '')
                username = None
                if '@' in request_uri:
                    # Extract username from request URI (e.g., /webdav/username@domain/Music/...)
                    uri_parts = request_uri.strip('/').split('/')
                    if len(uri_parts) >= 2 and uri_parts[0] == 'webdav' and '@' in uri_parts[1]:
                        username = uri_parts[1]
                
                # CRITICAL: Remove duplicate /webdav/ patterns first
                # Handle cases like /webdav/webdav/username/ -> /webdav/username/
                if '/webdav/webdav/' in href_text:
                    href_text = href_text.replace('/webdav/webdav/', '/webdav/')
                    needs_fixing = True
                
                # CRITICAL: Remove duplicate username patterns (use username from REQUEST_URI, not from href)
                # Handle cases like /webdav/username/webdav/username/ -> /webdav/username/
                if '@' in href_text and username:
                    username_pattern = re.escape(username)
                    # Remove duplicate /webdav/username/webdav/username/ pattern
                    pattern = r'/webdav/' + username_pattern + r'/webdav/' + username_pattern + r'/'
                    if re.search(pattern, href_text):
                        href_text = re.sub(pattern, f'/webdav/{username}/', href_text)
                        needs_fixing = True
                    # Also handle username/username/ pattern (if /webdav/ was stripped)
                    pattern2 = r'^/' + username_pattern + r'/' + username_pattern + r'/'
                    if re.search(pattern2, href_text):
                        href_text = re.sub(pattern2, f'/{username}/', href_text)
                        needs_fixing = True
                
                # CRITICAL: Ensure hrefs are absolute with /webdav/username@domain/ prefix
                # Flacbox needs absolute hrefs to construct GET requests
                if not href_text.startswith(script_name):
                    # Href doesn't have /webdav/ prefix - needs fixing
                    # Check if it's a relative path (no leading /, like "Music/song.mp3")
                    is_relative = not href_text.startswith('/')
                    
                    if is_relative:
                        # Relative path like "Music/song.mp3" - make it absolute
                        if username:
                            href_text = f"{script_name}/{username}/{href_text}"
                            needs_fixing = True
                        else:
                            href_text = f"{script_name}/{href_text}"
                            needs_fixing = True
                    elif href_text.startswith('/') and '@' not in href_text:
                        # Absolute path without username like "/Music/song.mp3"
                        if username:
                            href_text = f"{script_name}/{username}{href_text}"
                            needs_fixing = True
                        else:
                            href_text = script_name.rstrip('/') + href_text
                            needs_fixing = True
                    elif href_text.startswith('/') and '@' in href_text:
                        # Absolute path with username but missing /webdav/ like "/username@domain/Music/..."
                        href_text = script_name.rstrip('/') + href_text
                        needs_fixing = True
                
                # Update href if it changed (skip logging for large responses to reduce overhead)
                if needs_fixing and href_text != original_href:
                    href_elem.text = href_text
                    hrefs_fixed += 1
                    # Always log audio file href fixes
                    is_audio = any(ext in href_text.lower() or ext in original_href.lower() for ext in ['.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac'])
                    if (hrefs_fixed <= 5 and not is_large_response) or is_audio:
                        logger.info(f"[WebDAV] 🔧 Fixed href to absolute: '{original_href}' -> '{href_text}'")
            
            # CRITICAL: Ensure all property elements are properly namespaced with D: prefix
            # Flacbox requires explicitly prefixed elements (D:getcontenttype, not just getcontenttype)
            propstat = response.find("{DAV:}propstat")
            if propstat is not None:
                prop = propstat.find("{DAV:}prop")
                if prop is not None:
                    # Check if propstat has a status - some clients require this
                    status = propstat.find("{DAV:}status")
                    if status is None or not status.text:
                        # Add status if missing
                        status_elem = etree.SubElement(propstat, "{DAV:}status")
                        status_elem.text = "HTTP/1.1 200 OK"
                        props_fixed += 1
                    
                    # OPTIMIZATION: For large listings, skip expensive property namespace fixing
                    # Most properties should already be correctly namespaced from get_property_value
                    # Only fix properties for smaller responses or first batch
                    if not is_large_response or props_fixed < 50:
                        # Recreate unprefixed property elements with DAV namespace for Flacbox
                        elements_to_replace = []
                        for elem in prop:
                            # Check if element is unprefixed (no namespace URI)
                            if not elem.tag.startswith('{'):
                                elements_to_replace.append((elem, elem.tag, elem.text, list(elem)))
                        
                        # Replace unprefixed elements with namespaced versions
                        for old_elem, tag, text, children in elements_to_replace:
                            # Create new element with DAV namespace URI
                            new_elem = etree.Element(f"{{DAV:}}{tag}")
                            if text:
                                new_elem.text = text
                            # Copy children
                            for child in children:
                                new_elem.append(child)
                            # Replace in parent
                            prop.remove(old_elem)
                            prop.append(new_elem)
                            props_fixed += 1
        
        if hrefs_fixed > 0 or props_fixed > 0:
            if is_large_response:
                logger.debug(f"[WebDAV] ✅ Fixed {hrefs_fixed} hrefs and {props_fixed} property elements in XML response ({total_responses} total)")
            else:
                logger.info(f"[WebDAV] ✅ Fixed {hrefs_fixed} hrefs and {props_fixed} property elements in XML response")
    
    xml_bytes = etree.tostring(multistatus_elem, encoding='utf-8')
    xml_str = xml_bytes.decode('utf-8')

    # Check for audio files in response (for logging/debugging)
    # Sample first few audio files even for large responses
    audio_hrefs_in_xml = []
    audio_count = 0
    for response in all_responses:
        href_elem = response.find("{DAV:}href")
        if href_elem is not None and href_elem.text:
            href_text = href_elem.text
            if any(ext in href_text.lower() for ext in ['.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac']):
                audio_hrefs_in_xml.append(href_text)
                audio_count += 1
                if len(audio_hrefs_in_xml) >= 5:  # Log first 5
                    break
    
    # CRITICAL: Always log audio file hrefs to see what Flacbox receives
    if audio_hrefs_in_xml:
        # Check if hrefs are absolute (start with /webdav/)
        absolute_count = sum(1 for h in audio_hrefs_in_xml if h.startswith('/webdav/'))
        relative_count = len(audio_hrefs_in_xml) - absolute_count
        logger.info(f"[WebDAV] 🎵 Audio file hrefs in FINAL XML (total responses={total_responses}, audio files={audio_count}): {audio_hrefs_in_xml[:5]}")
        if relative_count > 0:
            logger.error(f"[WebDAV] ⚠️ CRITICAL: {relative_count}/{len(audio_hrefs_in_xml)} audio hrefs are NOT absolute (missing /webdav/ prefix)! Flacbox cannot play these!")
            logger.error(f"[WebDAV] ⚠️ Problematic hrefs: {[h for h in audio_hrefs_in_xml if not h.startswith('/webdav/')][:3]}")
        else:
            logger.info(f"[WebDAV] ✅ All audio hrefs are absolute (start with /webdav/)")
    
    # CRITICAL: Always log XML for audio files, especially for individual file PROPFIND (depth=0)
    # This is critical for debugging why Flacbox isn't making GET requests
    # For individual file requests (total_responses=1), always log the full XML
    # Also log for small directory listings (<= 10 items) to see structure
    if audio_hrefs_in_xml and (total_responses <= 10 or total_responses == 1):
        # Log a sample of the actual XML for one audio file to debug
        try:
            for response in all_responses:
                href_elem = response.find("{DAV:}href")
                if href_elem is not None and href_elem.text and any(ext in href_elem.text.lower() for ext in ['.mp3', '.m4a', '.flac']):
                    # Get all properties for this audio file
                    propstat = response.find("{DAV:}propstat")
                    if propstat is not None:
                        prop = propstat.find("{DAV:}prop")
                        if prop is not None:
                            # XML shows unprefixed elements like <getcontenttype> inside <D:prop>
                            # Try multiple approaches to find them
                            content_type = None
                            content_length = None
                            resourcetype = None
                            # Try with DAV namespace first
                            content_type = prop.find("{DAV:}getcontenttype")
                            content_length = prop.find("{DAV:}getcontentlength")
                            resourcetype = prop.find("{DAV:}resourcetype")
                            # If not found, try without namespace (unprefixed elements)
                            if content_type is None:
                                for elem in prop:
                                    if elem.tag.endswith('}getcontenttype') or elem.tag == 'getcontenttype':
                                        content_type = elem
                                        break
                            if content_length is None:
                                for elem in prop:
                                    if elem.tag.endswith('}getcontentlength') or elem.tag == 'getcontentlength':
                                        content_length = elem
                                        break
                            if resourcetype is None:
                                resourcetype = prop.find("resourcetype")
                            logger.info(f"[WebDAV] ⚠️  Audio file in XML: href={href_elem.text}, content-type={content_type.text if content_type is not None and content_type.text else 'MISSING'}, content-length={content_length.text if content_length is not None and content_length.text else 'MISSING'}, resourcetype={resourcetype.tag if resourcetype is not None else 'MISSING'}")
                            # Log the full response XML to see the complete structure Flacbox receives
                            try:
                                # Log the full response element (includes href, propstat, status)
                                response_xml = etree.tostring(response, encoding='utf-8').decode('utf-8')
                                logger.info(f"[WebDAV] 🔍 FULL response XML for audio file (what Flacbox sees):\n{response_xml}")
                                # Also log just the prop for reference
                                prop_xml = etree.tostring(prop, encoding='utf-8').decode('utf-8')
                                logger.info(f"[WebDAV] Prop XML: {prop_xml}")
                            except Exception as e:
                                logger.warning(f"[WebDAV] Failed to serialize XML: {e}")
                            break
                    else:
                        logger.warning(f"[WebDAV] No propstat found for audio file: {href_elem.text}")
                if href_elem is not None and href_elem.text and any(ext in href_elem.text.lower() for ext in ['.mp3', '.m4a', '.flac']):
                    # Also log the raw response XML structure
                    try:
                        response_xml = etree.tostring(response, encoding='utf-8').decode('utf-8')
                        logger.info(f"[WebDAV] Full response XML for audio file (first 800 chars): {response_xml[:800]}")
                    except Exception as e:
                        logger.warning(f"[WebDAV] Failed to serialize response XML: {e}")
                    break
        except Exception as e:
            logger.error(f"[WebDAV] Error logging audio file XML details: {e}", exc_info=True)
    elif total_responses > 0:
        # Log a sample of hrefs to see what's actually in the response
        sample_hrefs = []
        for response in list(multistatus_elem.findall("{DAV:}response"))[:5]:
            href_elem = response.find("{DAV:}href")
            if href_elem is not None and href_elem.text:
                sample_hrefs.append(href_elem.text)
        logger.debug(f"[WebDAV] PROPFIND response has {total_responses} responses, sample hrefs: {sample_hrefs}")

    # Check for .resource in the XML (Joplin resource files)
    if '.resource' in xml_str:
        # Count total .resource files in response
        resource_count = 0
        resource_responses = []
        for response in multistatus_elem.findall("{DAV:}response"):
            href_elem = response.find("{DAV:}href")
            if href_elem is not None and href_elem.text and '.resource' in href_elem.text:
                resource_count += 1
                # Count href elements in this response
                href_count = len(response.findall("{DAV:}href"))
                resource_responses.append((href_elem.text, href_count))
                # Only log as error if there are multiple href elements (actual problem)
                if href_count > 1:
                    logger.error(f"[WebDAV] ⚠️  MULTIPLE HREF ELEMENTS in .resource response: {href_elem.text}, count={href_count}")
                    for i, h in enumerate(response.findall("{DAV:}href")):
                        logger.error(f"[WebDAV]     href[{i}]: {h.text}")
        
        # Log summary at debug level (not error - this is normal for Joplin)
        if resource_count > 0:
            logger.debug(f"[WebDAV] XML Response contains {resource_count} .resource file(s) (normal for Joplin)")
            # Only log details if there were issues
            if any(count > 1 for _, count in resource_responses):
                logger.warning(f"[WebDAV] Some .resource responses have multiple href elements - this may cause Joplin sync errors")

    return _original_send_multi_status_response(environ, start_response, multistatus_elem)

wsgidav_util2.send_multi_status_response = _patched_send_multi_status_response

# Also patch the request_server module to intercept get_href calls
from wsgidav import request_server
_original_do_propfind = request_server.RequestServer.do_PROPFIND

def _patched_do_propfind(self, environ, start_response):
    """Patched PROPFIND - let wsgidav handle everything, rely on get_href() to return correct paths."""
    path = environ.get("PATH_INFO", "")
    depth = environ.get("HTTP_DEPTH", "1")  # Default depth is 1

    logger.debug(f"[WebDAV] PROPFIND: path={path}, depth={depth}")

    # Capture the response to log it
    captured_response = []
    def capturing_start_response(status, headers):
        captured_response.append((status, headers))
        return start_response(status, headers)

    # Call original method
    result = _original_do_propfind(self, environ, capturing_start_response)

    # Log a sample of the response for debugging
    if result and len(result) > 0:
        try:
            sample = result[0][:500] if isinstance(result[0], bytes) else str(result[0])[:500]
            logger.debug(f"[WebDAV] PROPFIND response sample: {sample}")
        except Exception:
            pass

    return result

# Apply the PROPFIND patch
request_server.RequestServer.do_PROPFIND = _patched_do_propfind
from app.services.storage_service import StorageService, get_storage_service
from app.auth import verify_password

# Global server instance
_webdav_server: Optional[WSGIServer] = None
_webdav_thread: Optional[threading.Thread] = None


class ResourcePathWrapper:
    """Wrapper that ensures resource.path always returns a string."""
    def __init__(self, wrapped_resource):
        object.__setattr__(self, '_wrapped', wrapped_resource)

    def __getattribute__(self, name):
        if name == '_wrapped':
            return object.__getattribute__(self, '_wrapped')
        wrapped = object.__getattribute__(self, '_wrapped')
        if name == 'path':
            path_value = getattr(wrapped, 'path')
            if isinstance(path_value, list):
                return str(path_value[0]) if len(path_value) > 0 else ''
            return str(path_value)
        return getattr(wrapped, name)

    def __setattr__(self, name, value):
        if name == '_wrapped':
            object.__setattr__(self, '_wrapped', value)
        else:
            setattr(object.__getattribute__(self, '_wrapped'), name, value)


class QuotaFilesystemProvider(FilesystemProvider):
    """Filesystem provider with quota checking and remote storage support."""

    def __init__(self, root_path: Path, db: Session):
        logger.debug(f"[WebDAV] QuotaFilesystemProvider.__init__ called with root_path={root_path}")
        super().__init__(root_path)
        self.db = db
        self.storage = get_storage_service(db)
        # Check if we need to proxy to remote storage
        self.storage_server_url = None
        self.storage_server_token = None
        storage_setting = db.query(Setting).filter(Setting.key == "storage_server_url").first()
        logger.info(f"[WebDAV] storage_setting query result: {storage_setting}, value={storage_setting.value if storage_setting else 'None'}")
        if storage_setting and storage_setting.value:
            url = storage_setting.value.strip()
            if url.startswith(('http://', 'https://')):
                self.storage_server_url = url
                # Get server-to-server token if available
                token_setting = db.query(Setting).filter(Setting.key == "storage_server_token").first()
                if token_setting and token_setting.value:
                    self.storage_server_token = token_setting.value
                    logger.info(f"[WebDAV] Remote storage server configured: {url} (with token)")
                else:
                    logger.warning(f"[WebDAV] Remote storage server configured: {url} (NO TOKEN - authentication may fail)")
                logger.info(f"[WebDAV] WebDAV will proxy ALL file operations to remote storage server")
                logger.info(f"[WebDAV] Local filesystem at {root_path} will NOT be used when remote storage is configured")

        # Initialize cache for directory listings to improve performance
        # Cache format: {(username, path, ...): (timestamp, items_list)}
        # Note: Different functions use different key formats:
        #   - _proxy_list_files: (username, path, depth) - 3-tuple
        #   - _proxy_get_info: (username, parent_path) - 2-tuple
        # Using Dict[Tuple, ...] to allow different tuple lengths
        self._dir_cache: Dict[Tuple, Tuple[float, list]] = {}
        self._cache_ttl = 60.0  # Cache directory listings for 60 seconds (increased for better performance with large dirs)
        # CRITICAL: Longer cache TTL for Music directory to reduce processing overhead
        self._music_cache_ttl = 300.0  # 5 minutes for Music directory (large directory, changes infrequently)
        self._cache_lock = threading.Lock()

        # Log storage path and verify it's correct
        logger.info(f"[WebDAV] QuotaFilesystemProvider initialized with root_path: {root_path}")
        logger.debug(f"[WebDAV] QuotaFilesystemProvider.__init__ completed")
        if root_path.exists():
            try:
                # Count files to verify this is the right location (only if local storage)
                if not self.storage_server_url:
                    file_count = sum(1 for _ in root_path.rglob('*') if _.is_file())
                    logger.info(f"[WebDAV] Root path contains {file_count} files")
            except Exception as e:
                logger.warning(f"[WebDAV] Could not count files in root_path: {e}")
    
    def create_collection(self, path: str):
        """Override to prevent creating directories with file names."""
        # Normalize path
        normalized_path = path.rstrip('/')
        
        # Check if this looks like a file (has extension)
        from pathlib import Path as PathLib
        path_obj = PathLib(normalized_path)
        if path_obj.suffix and path_obj.name != path_obj.stem:
            # This has a file extension - don't allow creating it as a directory
            logger.warning(f"[WebDAV] Attempted to create directory with file name: {normalized_path}")
            raise Exception(f"Cannot create directory with file name: {normalized_path}")
        
        # Check if a file exists at this path
        fs_path = self._locate_file_path(normalized_path)
        if fs_path and fs_path.exists() and fs_path.is_file():
            logger.warning(f"[WebDAV] File exists at directory path {normalized_path}")
            raise Exception(f"File exists at path: {normalized_path}")
        
        return super().create_collection(normalized_path)
    
    def _get_username_from_path(self, path: str) -> Optional[str]:
        """Extract username from WebDAV path."""
        # Path format: /username@domain/... or /webdav/username@domain/...
        # WebDAV clients (iOS/macOS) often use email format (username@domain) in paths
        # The storage server expects the FULL email-style username (e.g., verita84@poster.place)
        # Strip /webdav prefix if present (WSGI middleware might not strip it)
        normalized_path = path.strip('/')
        if normalized_path.startswith('webdav/'):
            normalized_path = normalized_path[7:]  # Remove 'webdav/'

        # Extract the first path component (which may be username@domain)
        if '/' in normalized_path:
            username = normalized_path.split('/', 1)[0]  # Split only on first /
        else:
            username = normalized_path

        if username:
            logger.debug(f"[WebDAV] Extracted username '{username}' from path '{path}' (normalized: '{normalized_path}')")
            return username
        logger.debug(f"[WebDAV] Could not extract username from path '{path}' (normalized: '{normalized_path}')")
        return None
    
    def _check_quota(self, username: str, additional_bytes: int = 0) -> tuple[bool, Optional[str]]:
        """Check if user has enough quota."""
        user = self.db.query(User).filter(User.username == username).first()
        if not user:
            return False, "User not found"
        
        # 0 means unlimited
        if user.storage_quota == 0:
            return True, None
        
        # Calculate current usage
        user_path = self.root_path / username
        current_usage = self._calculate_directory_size(user_path)
        
        if current_usage + additional_bytes > user.storage_quota:
            used_mb = current_usage / (1024 * 1024)
            quota_mb = user.storage_quota / (1024 * 1024)
            return False, f"Storage quota exceeded ({used_mb:.1f}MB / {quota_mb:.1f}MB)"
        
        return True, None
    
    def _calculate_directory_size(self, path: Path) -> int:
        """Calculate total size of directory in bytes."""
        if not path.exists():
            return 0
        
        total = 0
        try:
            for item in path.rglob('*'):
                if item.is_file():
                    total += item.stat().st_size
        except Exception as e:
            logger.warning(f"Error calculating directory size for {path}: {e}")
        
        return total
    
    def write_file_content(self, path: str, content: bytes, *, etag: Optional[str] = None):
        """Override to check quota and proxy to remote storage if configured."""
        import hashlib
        import re

        # Strip /webdav prefix if present
        path_stripped = path.strip('/')
        if path_stripped.startswith('webdav/'):
            path_stripped = '/' + path_stripped[7:]
        else:
            path_stripped = path
        # Normalize path - remove trailing slash if present (files shouldn't have trailing slashes)
        normalized_path = path_stripped.rstrip('/')

        # Handle chunked uploads - store chunks in /tmp
        chunk_match = re.search(r'\.chunk\.(\d+)$', normalized_path)
        if chunk_match:
            # This is a chunk file - store in /tmp
            chunk_num = chunk_match.group(1)
            # Create a unique identifier based on the final file path
            base_path = normalized_path.rsplit('.chunk.', 1)[0]
            chunk_id = hashlib.md5(base_path.encode()).hexdigest()
            tmp_chunk_path = Path(f"/tmp/webdav_chunks/{chunk_id}.chunk.{chunk_num}")
            tmp_chunk_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_chunk_path.write_bytes(content)
            logger.debug(f"[WebDAV] Stored chunk {chunk_num} in /tmp for {base_path}")
            return  # Don't write chunk to actual storage

        # Handle chunk metadata - combine chunks and write final file
        if normalized_path.endswith('.chunkmeta'):
            return self._handle_chunked_upload_complete(normalized_path, content)

        username = self._get_username_from_path(normalized_path)
        if username:
            # If remote storage is configured, ALWAYS proxy - never use local filesystem
            if self.storage_server_url:
                # Extract relative path
                rel_path = normalized_path.lstrip('/')
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path == username:
                    rel_path = ''

                # Proxy upload - this is the ONLY way when remote storage is configured
                try:
                    # Ensure parent directories exist before uploading
                    self._ensure_parent_directories_exist(username, rel_path)

                    self._proxy_upload_file(username, rel_path, content)
                    # Invalidate cache
                    self._invalidate_cache_for_path(username, normalized_path)
                    logger.debug(f"[WebDAV] Proxied file upload to storage server: {normalized_path} ({len(content)} bytes)")
                    return  # Success, don't write locally
                except Exception as e:
                    logger.error(f"[WebDAV] Failed to proxy upload to storage server: {e}")
                    # Don't fall back to local - raise the error
                    raise
            
            # Check quota (for local storage)
            allowed, error = self._check_quota(username, len(content))
            if not allowed:
                raise Exception(error or "Quota exceeded")
            
            # Get the actual filesystem path
            fs_path = self._locate_file_path(normalized_path)
            if fs_path and fs_path.exists() and fs_path.is_dir():
                # A directory exists with this name - remove it and create a file instead
                logger.warning(f"[WebDAV] Directory exists at file path {normalized_path}, removing and creating file")
                try:
                    fs_path.rmdir()  # Only works if directory is empty
                except OSError:
                    # Directory not empty - this is a problem, but try to continue
                    logger.error(f"[WebDAV] Cannot remove non-empty directory at {normalized_path}")
        
        result = super().write_file_content(normalized_path, content, etag=etag)

        # Invalidate file cache for parent directory
        if username:
            self._invalidate_cache_for_path(username, normalized_path)

        return result

    def _handle_chunked_upload_complete(self, meta_path: str, meta_content: bytes):
        """Handle chunked upload completion - combine chunks and write final file.

        Meta file format: num_chunks|total_size|md5_hash|final_path
        Chunks are stored in /tmp/webdav_chunks/{chunk_id}.chunk.{N}
        """
        import hashlib

        try:
            # Parse metadata
            meta_str = meta_content.decode('utf-8')
            parts = meta_str.split('|')
            if len(parts) != 4:
                raise ValueError(f"Invalid chunk metadata format: {meta_str}")

            num_chunks = int(parts[0])
            total_size = int(parts[1])
            expected_hash = parts[2]
            final_path = parts[3]

            logger.info(f"[WebDAV] Combining {num_chunks} chunks for {final_path} (expected size: {total_size}, MD5: {expected_hash})")

            # Create chunk ID from final path
            chunk_id = hashlib.md5(final_path.encode()).hexdigest()

            # Combine chunks in /tmp and compute hash while streaming
            combined_path = Path(f"/tmp/webdav_chunks/{chunk_id}.combined")
            combined_path.parent.mkdir(parents=True, exist_ok=True)

            # Read and combine all chunks, computing hash as we go
            actual_hash = hashlib.md5()
            actual_size = 0
            with open(combined_path, 'wb') as combined:
                for i in range(num_chunks):
                    chunk_path = Path(f"/tmp/webdav_chunks/{chunk_id}.chunk.{i}")
                    if not chunk_path.exists():
                        raise FileNotFoundError(f"Missing chunk {i} for {final_path}")
                    # Read chunk in smaller pieces to avoid memory issues
                    with open(chunk_path, 'rb') as chunk_file:
                        while True:
                            data = chunk_file.read(65536)  # 64KB at a time
                            if not data:
                                break
                            combined.write(data)
                            actual_hash.update(data)
                            actual_size += len(data)
                    logger.debug(f"[WebDAV] Combined chunk {i + 1}/{num_chunks} for {final_path}")

            # Verify size and hash
            if actual_size != total_size:
                raise ValueError(f"Size mismatch: expected {total_size}, got {actual_size}")

            if actual_hash.hexdigest() != expected_hash:
                raise ValueError(f"Checksum mismatch: expected {expected_hash}, got {actual_hash.hexdigest()}")

            logger.info(f"[WebDAV] Chunk verification passed for {final_path}")

            # Write the final file
            username = self._get_username_from_path(final_path)
            if username and self.storage_server_url:
                # Extract relative path for proxy upload
                rel_path = final_path.lstrip('/')
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path == username:
                    rel_path = ''
                # Ensure parent directories exist before uploading
                self._ensure_parent_directories_exist(username, rel_path)
                # Stream from combined file for proxy upload
                with open(combined_path, 'rb') as f:
                    combined_data = f.read()
                self._proxy_upload_file(username, rel_path, combined_data)
                self._invalidate_cache_for_path(username, final_path)
            else:
                # Local storage - stream from combined file
                with open(combined_path, 'rb') as f:
                    combined_data = f.read()
                super().write_file_content(final_path, combined_data)
                if username:
                    self._invalidate_cache_for_path(username, final_path)

            logger.info(f"[WebDAV] Chunked upload complete: {final_path} ({actual_size} bytes)")

            # Clean up chunks and combined file
            for i in range(num_chunks):
                chunk_path = Path(f"/tmp/webdav_chunks/{chunk_id}.chunk.{i}")
                try:
                    chunk_path.unlink()
                except:
                    pass
            try:
                combined_path.unlink()
            except:
                pass

        except Exception as e:
            logger.error(f"[WebDAV] Failed to complete chunked upload: {e}")
            raise

    def _proxy_create_directory(self, username: str, directory_path: str):
        """Create a directory on the remote storage server."""
        import requests

        if not self.storage_server_url:
            raise Exception("storage_server_url not configured")

        url = f"{self.storage_server_url.rstrip('/')}/api/storage/mkdir"
        headers = {}
        if self.storage_server_token:
            headers["Authorization"] = f"Bearer {self.storage_server_token}"

        data = {
            'username': username,
            'path': directory_path
        }

        try:
            response = requests.post(url, headers=headers, data=data, timeout=30)
            # 400 with "Directory already exists" is OK - we just want to ensure it exists
            if response.status_code == 400 and "already exists" in response.text:
                logger.debug(f"[WebDAV] Directory already exists: {directory_path}")
                return
            response.raise_for_status()
            logger.info(f"[WebDAV] Created directory: {directory_path}")

            # Invalidate cache for the parent directory
            from pathlib import Path
            parent_path = str(Path(directory_path).parent) if Path(directory_path).parent != Path('.') else ''
            self._invalidate_cache(username, parent_path)
        except Exception as e:
            logger.error(f"[WebDAV] Failed to create directory {directory_path}: {e}")
            raise

    def _ensure_parent_directories_exist(self, username: str, file_path: str):
        """Ensure all parent directories exist, creating them recursively if needed."""
        from pathlib import Path

        path_obj = Path(file_path)
        parent = path_obj.parent

        # If parent is '.' (current directory), no need to create anything
        if parent == Path('.'):
            return

        # Get all parent directories from root to immediate parent
        parts = str(parent).split('/')
        current_path = ''

        for part in parts:
            if not part:  # Skip empty parts
                continue

            # Build path incrementally (e.g., "Joplin", "Joplin/.resource")
            current_path = f"{current_path}/{part}" if current_path else part

            # Check if this directory exists
            try:
                info = self._proxy_get_info(username, current_path)
                if info and info.get('is_directory'):
                    logger.debug(f"[WebDAV] Directory exists: {current_path}")
                    continue  # Directory exists, move to next level
                elif info and not info.get('is_directory'):
                    # A file exists with this name - this is a problem
                    logger.error(f"[WebDAV] Cannot create directory {current_path} - a file exists with this name")
                    raise Exception(f"Cannot create directory {current_path} - a file exists with this name")
            except Exception:
                # Directory doesn't exist or error checking - assume it doesn't exist
                pass

            # Directory doesn't exist - create it
            try:
                logger.info(f"[WebDAV] Creating parent directory: {current_path}")
                self._proxy_create_directory(username, current_path)
            except Exception as e:
                logger.error(f"[WebDAV] Failed to create parent directory {current_path}: {e}")
                raise

    def _invalidate_cache(self, username: str, path: str = None):
        """Invalidate cache for a specific directory or all user directories."""
        with self._cache_lock:
            if path is not None:
                # Invalidate specific directory
                cache_key = (username, path)
                if cache_key in self._dir_cache:
                    del self._dir_cache[cache_key]
                    logger.debug(f"[WebDAV] Invalidated cache for {username}/{path}")
            else:
                # Invalidate all directories for this user
                keys_to_remove = [k for k in self._dir_cache.keys() if k[0] == username]
                for key in keys_to_remove:
                    del self._dir_cache[key]
                logger.debug(f"[WebDAV] Invalidated all cache entries for user {username}")

    def _proxy_upload_file(self, username: str, file_path: str, content: bytes):
        """Proxy file upload - calls storage server directly."""
        import requests

        # Call the REMOTE storage server directly (not local API)
        if not self.storage_server_url:
            raise Exception("storage_server_url not configured")

        url = f"{self.storage_server_url.rstrip('/')}/api/storage/upload-file"
        headers = {}
        if self.storage_server_token:
            headers["Authorization"] = f"Bearer {self.storage_server_token}"

        # Split file_path into directory and filename
        # Storage API expects 'path' to be the directory, and filename comes from the uploaded file
        from pathlib import Path
        path_obj = Path(file_path)
        filename = path_obj.name
        directory = str(path_obj.parent) if path_obj.parent != Path('.') else ''

        # Determine content type
        ext = path_obj.suffix
        content_type = 'application/octet-stream'
        if ext == '.txt' or ext == '.md':
            content_type = 'text/plain'
        elif ext == '.json':
            content_type = 'application/json'
        elif ext in ['.jpg', '.jpeg']:
            content_type = 'image/jpeg'
        elif ext == '.png':
            content_type = 'image/png'

        files = {
            'file': (filename, content, content_type)
        }
        data = {
            'username': username,
            'path': directory  # Send only the directory path, not the full file path
        }

        response = requests.post(url, headers=headers, files=files, data=data, timeout=30)
        response.raise_for_status()

        # Invalidate cache for the directory
        self._invalidate_cache(username, directory)

        return response.json()
    
    def _locate_file_path(self, path: str) -> Optional[Path]:
        """Get the filesystem path for a WebDAV path."""
        try:
            # Remove leading slash and split
            parts = path.strip('/').split('/')
            if not parts or not parts[0]:
                return None
            
            # Build filesystem path
            fs_path = self.root_path
            for part in parts:
                if not part:  # Skip empty parts
                    continue
                fs_path = fs_path / part
            
            return fs_path
        except Exception:
            return None
    
    def get_resource_list(self, path: str, depth: int = 1, environ: dict = None):
        """Override to list files, with support for remote storage proxying."""
        logger.debug(f"[WebDAV] get_resource_list CALLED: path={path}, depth={depth}, storage_server_url={self.storage_server_url}")
        
        # Strip /webdav prefix if present (may be added multiple times)
        original_path = path
        normalized_path = path.strip('/')
        # Remove all /webdav/ prefixes (in case it's duplicated)
        while normalized_path.startswith('webdav/'):
            normalized_path = normalized_path[7:]  # Remove 'webdav/'

        # CRITICAL FIX: Remove embedded /webdav/username/ duplication
        # Example: /user@domain/webdav/user@domain/path -> /user@domain/path
        import re
        # Match pattern: /username/webdav/username/ and replace with /username/
        # This handles cases where the client duplicates the path
        normalized_path = re.sub(r'^([^/]+)/webdav/\1/', r'\1/', normalized_path)

        # Restore leading / if we have a path
        if normalized_path:
            normalized_path = '/' + normalized_path
        else:
            normalized_path = '/'

        logger.debug(f"[WebDAV] get_resource_list: original={original_path}, normalized={normalized_path}, depth={depth}")
        
        # If remote storage is configured, ALWAYS proxy - never use local filesystem
        if self.storage_server_url:
            username = self._get_username_from_path(normalized_path)
            logger.debug(f"[WebDAV] Remote storage configured: {self.storage_server_url}, username={username}, path={normalized_path}")
            if username:
                # Extract relative path from WebDAV path
                # Path format: /username/subdir -> subdir
                rel_path = normalized_path.lstrip('/')
                
                # CRITICAL: Remove duplicate username patterns first
                # Handle cases like: username/username/path -> username/path
                # or: username@domain/username@domain/path -> username@domain/path
                username_prefix = username + '/'
                if rel_path.startswith(username_prefix + username_prefix):
                    # Remove the first occurrence
                    rel_path = rel_path[len(username) + 1:]
                    logger.warning(f"[WebDAV] Removed duplicate username pattern in get_resource_list, new rel_path={rel_path}")
                elif rel_path.startswith(username_prefix + username + '/'):
                    # Handle case where there's a duplicate but with different separator
                    rel_path = rel_path[len(username) + 1:]
                    logger.warning(f"[WebDAV] Removed duplicate username pattern (variant) in get_resource_list, new rel_path={rel_path}")
                
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path == username:
                    rel_path = ''

                # Additional check: remove any remaining webdav/username/ prefix
                # This handles cases where path normalization didn't catch everything
                if rel_path.startswith(f'webdav/{username}/'):
                    rel_path = rel_path[len(f'webdav/{username}/'):]
                    logger.debug(f"[WebDAV] get_resource_list: removed duplicate webdav/{username}/, rel_path={rel_path}")
                elif rel_path == f'webdav/{username}':
                    rel_path = ''
                    logger.debug(f"[WebDAV] get_resource_list: removed duplicate webdav/{username}, rel_path is now empty")
                
                # First check if this is a file (not a directory) - if so, return empty list
                # We can't list files inside a file
                try:
                    info = self._proxy_get_info(username, rel_path)
                    if info and not info.get('is_directory', False):
                        logger.debug(f"[WebDAV] Path {normalized_path} is a file, not a directory - returning empty list")
                        return []
                except Exception as e:
                    logger.debug(f"[WebDAV] Could not get info for {normalized_path}, assuming directory: {e}")
                    # Continue to try listing - might be a directory
                
                # Proxy to storage server - this is the ONLY way when remote storage is configured
                # Pass depth to support Depth: infinity PROPFIND requests
                # CRITICAL: Pass environ so resources can get SCRIPT_NAME for href generation
                try:
                    result = self._proxy_list_files(username, rel_path, depth=depth, environ=environ)
                    logger.info(f"[WebDAV] Proxied list returned {len(result)} items for {username}/{rel_path} (depth={depth})")
                    if len(result) == 0:
                        logger.warning(f"[WebDAV] Proxy returned 0 items - check if storage_server_url ({self.storage_server_url}) is correct")
                    return result
                except Exception as e:
                    logger.error(f"[WebDAV] Failed to proxy list to storage server: {e}", exc_info=True)
                    # If it's a 400 error and the path looks like a file, return empty list
                    if "400" in str(e) and (rel_path.endswith('.log') or rel_path.endswith('.ics') or '.' in rel_path.split('/')[-1]):
                        logger.warning(f"[WebDAV] Path {rel_path} appears to be a file (400 error), returning empty list")
                        return []
                    # Don't fall back to local - raise the error
                    raise
        
        # Only use local filesystem if remote storage is NOT configured
        logger.info(f"[WebDAV] Using local filesystem (no remote storage configured)")
        result = super().get_resource_list(normalized_path, depth, environ)
        logger.debug(f"[WebDAV] Parent get_resource_list returned: type={type(result)}, len={len(result) if hasattr(result, '__len__') else 'N/A'}")
        # IMPORTANT: Wrap all returned resources to ensure path is always a string
        if result and isinstance(result, list):
            result = [ResourcePathWrapper(r) if r else None for r in result]
        return result
    
    def get_resource_instances(self, path: str, environ: dict = None):
        """Override to handle resource instances - wsgidav calls this to get children of a resource."""
        import traceback
        logger.debug(f"[WebDAV] get_resource_instances CALLED: path={path}")
        
        # Strip /webdav prefix if present
        normalized_path = path.strip('/')
        while normalized_path.startswith('webdav/'):
            normalized_path = normalized_path[7:]
        if normalized_path:
            normalized_path = '/' + normalized_path
        else:
            normalized_path = '/'
        
        # get_resource_instances should return a list of child resources
        # This is what we implemented in get_resource_list
        try:
            return self.get_resource_list(normalized_path, depth=1, environ=environ)
        except Exception as e:
            logger.error(f"[WebDAV] get_resource_instances error: {e}", exc_info=True)
            raise
    
    def get_resource_inst(self, path: str, environ: dict = None):
        """Override get_resource_inst - wsgidav calls this for PROPFIND and GET requests."""
        # Log request method for debugging
        request_method = environ.get('REQUEST_METHOD', 'UNKNOWN') if environ else 'UNKNOWN'
        logger.info(f"[WebDAV] get_resource_inst CALLED: method={request_method}, path={path}, storage_server_url={self.storage_server_url}")
        
        # CRITICAL: Normalize path and remove duplicate patterns BEFORE processing
        # Handle cases like: /webdav/username/webdav/username/path -> /username/path
        import re
        original_path = path
        
        # First, normalize the path string
        normalized_path = path.strip('/')
        
        # CRITICAL: Remove ALL /webdav/ prefixes first (middleware already stripped it)
        # But handle cases where it wasn't stripped or got duplicated
        # Also handle /webdav/webdav/ patterns in the middle
        while normalized_path.startswith('webdav/'):
            normalized_path = normalized_path[7:]
        # Remove /webdav/webdav/ patterns anywhere in the path
        while '/webdav/webdav/' in normalized_path:
            normalized_path = normalized_path.replace('/webdav/webdav/', '/webdav/')
        while 'webdav/webdav/' in normalized_path:
            normalized_path = normalized_path.replace('webdav/webdav/', 'webdav/')
        
        # CRITICAL: Handle duplicate username patterns
        # Pattern: username/webdav/username/path -> username/path
        # Pattern: username/username/path -> username/path
        # Pattern: username/webdav/webdav/username/path -> username/path
        if '@' in normalized_path:
            # Find username pattern (e.g., "verita84@poster.place")
            username_match = re.search(r'([^/]+@[^/]+)', normalized_path)
            if username_match:
                username = username_match.group(1)
                username_pattern = re.escape(username)
                
                # Remove duplicate patterns in order (most specific first):
                # Use re.sub directly - it will only replace if pattern matches
                # 1. username/webdav/webdav/username/ -> username/
                pattern1 = r'^' + username_pattern + r'/webdav/webdav/' + username_pattern + r'/'
                if re.search(pattern1, normalized_path):
                    normalized_path = re.sub(pattern1, username + '/', normalized_path)
                    logger.warning(f"[WebDAV] Removed username/webdav/webdav/username/ pattern: '{original_path}' -> '{normalized_path}'")
                # 2. username/webdav/username/ -> username/
                pattern2 = r'^' + username_pattern + r'/webdav/' + username_pattern + r'/'
                if re.search(pattern2, normalized_path):
                    normalized_path = re.sub(pattern2, username + '/', normalized_path)
                    logger.warning(f"[WebDAV] Removed username/webdav/username/ pattern: '{original_path}' -> '{normalized_path}'")
                # 3. username/username/ -> username/
                pattern3 = r'^' + username_pattern + r'/' + username_pattern + r'/'
                if re.search(pattern3, normalized_path):
                    normalized_path = re.sub(pattern3, username + '/', normalized_path)
                    logger.warning(f"[WebDAV] Removed username/username/ pattern: '{original_path}' -> '{normalized_path}'")
        
        # Ensure path starts with /
        if normalized_path:
            normalized_path = '/' + normalized_path
        else:
            normalized_path = '/'
        
        if original_path != normalized_path:
            logger.warning(f"[WebDAV] Normalized duplicate path: '{original_path}' -> '{normalized_path}'")
        
        logger.debug(f"[WebDAV] Normalized path={normalized_path}, storage_server_url={self.storage_server_url}")
        
        # For remote storage, we MUST proxy - never use local filesystem
        if self.storage_server_url:
            logger.debug(f"[WebDAV] Remote storage configured, proxying to remote storage")
            username = self._get_username_from_path(normalized_path)
            if username:
                # Extract relative path
                rel_path = normalized_path.lstrip('/')
                logger.debug(f"[WebDAV] get_resource_inst: after lstrip, rel_path={rel_path}, username={username}")

                # CRITICAL: Remove ALL duplicate patterns aggressively
                import re
                username_pattern = re.escape(username)
                
                # Remove duplicate patterns in order:
                # 1. webdav/username/webdav/username/ -> username/
                if rel_path.startswith('webdav/' + username + '/webdav/' + username + '/'):
                    rel_path = rel_path[len('webdav/' + username + '/webdav/' + username + '/'):]
                    logger.warning(f"[WebDAV] Removed webdav/username/webdav/username/ pattern, new rel_path={rel_path}")
                # 2. username/webdav/username/ -> username/
                elif rel_path.startswith(username + '/webdav/' + username + '/'):
                    rel_path = rel_path[len(username + '/webdav/' + username + '/'):]
                    logger.warning(f"[WebDAV] Removed username/webdav/username/ pattern, new rel_path={rel_path}")
                # 3. username/username/ -> (empty, then add back username/)
                elif rel_path.startswith(username + '/' + username + '/'):
                    rel_path = rel_path[len(username + '/' + username + '/'):]
                    logger.warning(f"[WebDAV] Removed username/username/ pattern, new rel_path={rel_path}")
                # 4. webdav/username/ -> (remove webdav/)
                elif rel_path.startswith('webdav/' + username + '/'):
                    rel_path = rel_path[len('webdav/' + username + '/'):]
                    logger.debug(f"[WebDAV] Removed webdav/username/ prefix, new rel_path={rel_path}")
                # 5. username/ -> (remove username/)
                elif rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username + '/'):]
                    logger.debug(f"[WebDAV] Removed username/ prefix, new rel_path={rel_path}")
                # 6. Just username -> empty
                elif rel_path == username:
                    rel_path = ''
                    logger.debug(f"[WebDAV] Path was just username, set to empty")
                # 7. webdav/username -> empty
                elif rel_path == 'webdav/' + username:
                    rel_path = ''
                    logger.debug(f"[WebDAV] Path was webdav/username, set to empty")
                elif rel_path.startswith('calendar/dav/' + username + '/'):
                    rel_path = rel_path[len('calendar/dav/' + username) + 1:]
                    logger.debug(f"[WebDAV] get_resource_inst: matched calendar/dav/username/, rel_path={rel_path}")
                elif rel_path == 'calendar/dav/' + username:
                    rel_path = ''
                    logger.debug(f"[WebDAV] get_resource_inst: matched calendar/dav/username exactly, rel_path={rel_path}")
                elif rel_path.startswith('calendar/' + username + '/'):
                    rel_path = rel_path[len('calendar/' + username) + 1:]
                    logger.debug(f"[WebDAV] get_resource_inst: matched calendar/username/, rel_path={rel_path}")
                elif rel_path == 'calendar/' + username:
                    rel_path = ''
                    logger.debug(f"[WebDAV] get_resource_inst: matched calendar/username exactly, rel_path={rel_path}")
                else:
                    logger.warning(f"[WebDAV] Could not extract relative path from '{normalized_path}' (rel_path='{rel_path}') with username '{username}'")
                    # Try to normalize by removing any webdav/username prefix that might still be there
                    if '/webdav/' + username in rel_path:
                        # Handle cases like "username/webdav/username/file.jpg"
                        parts = rel_path.split('/webdav/' + username + '/')
                        if len(parts) > 1:
                            rel_path = parts[-1]  # Take the part after the duplicate
                            logger.warning(f"[WebDAV] Removed duplicate /webdav/username/, new rel_path={rel_path}")
                        else:
                            rel_path = ''
                    else:
                        rel_path = ''
                
                # Normalize rel_path - remove any leading/trailing slashes and whitespace
                rel_path = rel_path.strip(' /')
                
                logger.info(f"[WebDAV] get_resource_inst: method={request_method}, username={username}, rel_path={rel_path}, normalized_path={normalized_path}")
                
                # Get resource info from remote storage
                try:
                    # Define VirtualResource class before using it
                    # Ensure time module is accessible for nested class (closure needs it in function scope)
                    _ = time  # Reference time in function scope

                    class VirtualResource(_DAVResource):
                            def __init__(self, path, info_dict, provider, environ=None):
                                try:
                                    # Ensure path is always a string, never a list
                                    path_str = str(path[0]) if isinstance(path, list) and len(path) > 0 else str(path)
                                    # Manually set required attributes without calling super().__init__
                                    # This gives us full control over path
                                    if environ is None:
                                        environ = {"wsgidav.provider": provider} if provider else {}
                                    is_dir = info_dict.get('is_directory', False)

                                    # CRITICAL: Directories MUST have trailing slashes for WebDAV compliance
                                    # Many clients (like Flacbox) require this to recognize directories
                                    if is_dir and path_str and not path_str.endswith('/'):
                                        path_str = path_str + '/'
                                        logger.debug(f"[WebDAV] Added trailing slash to directory path in VirtualResource.__init__: {path_str}")

                                    # Set attributes directly using object.__setattr__ to avoid any property issues
                                    object.__setattr__(self, 'provider', provider)
                                    object.__setattr__(self, 'path', path_str)
                                    object.__setattr__(self, 'is_collection', is_dir)
                                    object.__setattr__(self, 'environ', environ)
                                    object.__setattr__(self, 'name', path_str.split('/')[-1] if path_str else '')
                                    object.__setattr__(self, '_path', path_str)
                                    object.__setattr__(self, '_info', info_dict)
                                    object.__setattr__(self, '_is_dir', is_dir)
                                    object.__setattr__(self, '_modified', info_dict.get('modified', time.time()))
                                    object.__setattr__(self, '_size', info_dict.get('size', 0))
                                    object.__setattr__(self, '_provider', provider)
                                    object.__setattr__(self, '_environ', environ)
                                    logger.debug(f"[WebDAV] VirtualResource.__init__ completed successfully for {path_str}")
                                except Exception as e:
                                    logger.error(f"[WebDAV] VirtualResource.__init__ FAILED: {e}", exc_info=True)
                                    raise

                            def get_last_modified(self):
                                return float(self._modified) if isinstance(self._modified, (int, float)) else time.time()
                            
                            def get_content_length(self):
                                return self._size if not self._is_dir else 0

                            def get_display_name(self):
                                return self._path.split('/')[-1] or self._path
                            
                            def get_ref_url(self):
                                # Return full path including /webdav/ prefix
                                ref_url = str(self._path)
                                if not ref_url.startswith('/webdav/'):
                                    ref_url = '/webdav' + ref_url
                                return ref_url

                            def get_href(self):
                                """Return href (URL path) for the resource."""
                                # CRITICAL: Extract string from list if _path is somehow a list
                                if isinstance(self._path, list):
                                    logger.error(f"[WebDAV] ⚠️  CRITICAL: VirtualResource._path is a list: {self._path}")
                                    href = str(self._path[0]) if len(self._path) > 0 else '/'
                                else:
                                    href = str(self._path)

                                # CRITICAL: Remove any existing /webdav/ prefix to normalize
                                if href.startswith('/webdav/'):
                                    href = href[7:]  # Remove '/webdav/'
                                elif href.startswith('/webdav'):
                                    href = href[7:]  # Remove '/webdav'
                                
                                # CRITICAL: Remove duplicate username patterns from href
                                import re
                                path_parts = href.strip('/').split('/')
                                if len(path_parts) > 0 and '@' in path_parts[0]:
                                    username = path_parts[0]
                                    username_pattern = re.escape(username) + r'/'
                                    # Remove duplicate username/username/ or username/webdav/username/ patterns
                                    href = re.sub(r'^/' + username_pattern + r'(webdav/)?' + username_pattern, f'/{username}/', href)
                                
                                # CRITICAL: Directories MUST end with trailing slash for WebDAV compliance
                                # Many clients (like Flacbox) require this to recognize directories
                                if self.is_collection and not href.endswith('/'):
                                    href = href + '/'
                                    logger.debug(f"[WebDAV] Added trailing slash to directory href: {href}")

                                # CRITICAL: WSGiDAV is NOT reliably prepending SCRIPT_NAME to hrefs in XML responses
                                # We must include /webdav/ prefix explicitly to ensure consistent absolute paths
                                script_name = self._environ.get('SCRIPT_NAME', '/webdav') if hasattr(self, '_environ') and self._environ else '/webdav'
                                if script_name and not href.startswith(script_name):
                                    # Ensure href starts with / (absolute path)
                                    if not href.startswith('/'):
                                        href = '/' + href
                                    # Prepend /webdav/ to make it absolute
                                    href = script_name.rstrip('/') + href
                                
                                # CRITICAL: Do NOT URL-encode @ symbol - Joplin expects @ in hrefs to match base URL
                                # The HTTP layer will handle URL encoding when needed
                                # href = href.replace('@', '%40')  # REMOVED - causes Joplin sync errors
                                
                                script_name_check = self._environ.get('SCRIPT_NAME', '') if hasattr(self, '_environ') and self._environ else ''
                                # Changed to debug to avoid logging every single file in large directories (e.g., 1900+ songs)
                                logger.debug(f"[WebDAV] VirtualResource.get_href returning: {href} (script_name={script_name_check})")
                                return href

                            def get_preferred_path(self):
                                """Return preferred path for this resource."""
                                if self._path in ("", "/"):
                                    return "/"
                                # Append '/' for collections
                                if self.is_collection and not self._path.endswith("/"):
                                    return self._path + "/"
                                return self._path

                            def get_etag(self):
                                return None

                            def get_content_type(self):
                                """Return proper MIME type for the resource."""
                                if self._is_dir:
                                    return 'httpd/unix-directory'
                                
                                # Get file extension from path
                                path_str = str(self._path) if hasattr(self, '_path') else ''
                                if '.' in path_str:
                                    ext = path_str.rsplit('.', 1)[-1].lower()
                                    # Common audio MIME types
                                    audio_mime_types = {
                                        'mp3': 'audio/mpeg',
                                        'm4a': 'audio/mp4',
                                        'm4b': 'audio/mp4',
                                        'aac': 'audio/aac',
                                        'flac': 'audio/flac',
                                        'ogg': 'audio/ogg',
                                        'oga': 'audio/ogg',
                                        'wav': 'audio/wav',
                                        'wma': 'audio/x-ms-wma',
                                        'opus': 'audio/opus',
                                    }
                                    if ext in audio_mime_types:
                                        return audio_mime_types[ext]
                                
                                # Default to octet-stream for unknown types
                                return 'application/octet-stream'

                            def get_content(self):
                                """Return file content as file-like object from remote storage.

                                WsgiDAV expects get_content() to return a file-like object (not bytes)
                                that supports read() method.
                                """
                                import io
                                from wsgidav.dav_error import DAVError, HTTP_INTERNAL_ERROR, HTTP_NOT_FOUND
                                if self._is_dir:
                                    return None
                                # Extract username and relative path
                                # Storage server expects full email-style username (e.g., verita84@poster.place)
                                path = self._path.strip('/')
                                logger.debug(f"[WebDAV] VirtualResource.get_content: self._path={self._path}, stripped path={path}")

                                # Remove all webdav/ prefixes (in case path is duplicated)
                                while path.startswith('webdav/'):
                                    path = path[7:]
                                    logger.debug(f"[WebDAV] VirtualResource.get_content: after removing webdav/ prefix: {path}")

                                parts = path.split('/', 1)
                                if len(parts) >= 2:
                                    username = parts[0]
                                    rel_path = parts[1]
                                elif len(parts) == 1:
                                    username = parts[0]
                                    rel_path = ''
                                else:
                                    return None

                                # Normalize rel_path - remove duplicate webdav/username/ prefix if present
                                # Example: "webdav/verita84@poster.place/avatar.jpeg" -> "avatar.jpeg"
                                if rel_path.startswith('webdav/' + username + '/'):
                                    rel_path = rel_path[len('webdav/' + username + '/'):]
                                    logger.debug(f"[WebDAV] VirtualResource.get_content: removed duplicate webdav/username/ prefix, rel_path={rel_path}")
                                elif rel_path == 'webdav/' + username:
                                    rel_path = ''
                                    logger.debug(f"[WebDAV] VirtualResource.get_content: path was just webdav/username, set to empty")

                                logger.info(f"[WebDAV] VirtualResource.get_content: username={username}, rel_path={rel_path}")
                                try:
                                    content = self._provider._proxy_download_file(username, rel_path)
                                    logger.info(f"[WebDAV] VirtualResource.get_content: downloaded {len(content)} bytes for {rel_path}")
                                    # Return file-like object, not raw bytes
                                    return io.BytesIO(content)
                                except Exception as e:
                                    error_str = str(e)
                                    logger.error(f"[WebDAV] VirtualResource.get_content error: {e}")
                                    # Return 404 for not found errors, 500 for others
                                    if "404" in error_str or "Not Found" in error_str:
                                        raise DAVError(HTTP_NOT_FOUND, f"File not found: {rel_path}")
                                    # Raise DAVError instead of returning None to avoid AttributeError on close()
                                    raise DAVError(HTTP_INTERNAL_ERROR, f"Failed to retrieve file content: {e}")

                            def support_content_length(self):
                                """Return True if get_content_length() returns valid value."""
                                return not self._is_dir

                            def support_ranges(self):
                                """Return True to support range requests for audio/video streaming."""
                                return True

                            def support_modified(self):
                                """Return True if get_last_modified() returns a valid value."""
                                return True

                            def support_etag(self):
                                """Return True if get_etag() returns a valid value."""
                                return False

                            def begin_write(self, *, content_type=None):
                                """Open content for writing - return a file-like object."""
                                import io
                                logger.debug(f"[WebDAV] VirtualResource.begin_write called for {self._path}")
                                if self._is_dir:
                                    from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN
                                    raise DAVError(HTTP_FORBIDDEN, "Cannot write to a directory")

                                # Create a custom buffer that captures content before close()
                                class CaptureBuffer(io.BytesIO):
                                    def __init__(self, resource):
                                        super().__init__()
                                        self._resource = resource
                                        self._captured_content = None

                                    def close(self):
                                        # Capture content before closing
                                        if not self._captured_content:
                                            self._captured_content = self.getvalue()
                                            self._resource._write_content = self._captured_content
                                        super().close()

                                self._write_buffer = CaptureBuffer(self)
                                return self._write_buffer

                            def end_write(self, *, with_errors):
                                """Called after writing is complete."""
                                logger.debug(f"[WebDAV] VirtualResource.end_write called for {self._path}, with_errors={with_errors}")
                                if with_errors:
                                    logger.error(f"[WebDAV] Write completed with errors for {self._path}")
                                    return
                                # Get the captured content (stored by close() in CaptureBuffer)
                                if hasattr(self, '_write_content'):
                                    content = self._write_content
                                    logger.debug(f"[WebDAV] Writing {len(content)} bytes to {self._path}")
                                    try:
                                        self._provider.write_file_content(self._path, content)
                                        logger.info(f"[WebDAV] Successfully wrote {len(content)} bytes to {self._path}")
                                    except Exception as e:
                                        logger.error(f"[WebDAV] Failed to write to {self._path}: {e}", exc_info=True)
                                        from wsgidav.dav_error import DAVError, HTTP_INTERNAL_ERROR
                                        raise DAVError(HTTP_INTERNAL_ERROR, f"Failed to write file: {e}")
                                    finally:
                                        if hasattr(self, '_write_content'):
                                            delattr(self, '_write_content')
                                        if hasattr(self, '_write_buffer'):
                                            delattr(self, '_write_buffer')
                                else:
                                    logger.warning(f"[WebDAV] end_write called but no content captured for {self._path}")

                            def finalize_headers(self, environ, response_headers):
                                """Called by WsgiDAV to allow the resource to modify response headers.

                                Return the (possibly modified) response headers list.
                                """
                                return response_headers

                            def get_descendants(self, depth=1, add_self=False, depth_first=False):
                                # Get children by calling get_resource_list directly
                                # wsgidav calls get_descendants on the resource to get children
                                # IMPORTANT: Pass the actual depth to support Depth: infinity requests
                                logger.debug(f"[WebDAV] VirtualResource.get_descendants CALLED: path={self._path}, depth={depth}, add_self={add_self}, depth_first={depth_first}")

                                # If this is a file (not a directory), return empty list
                                if not self._is_dir:
                                    logger.debug(f"[WebDAV] VirtualResource.get_descendants: {self._path} is a file, returning empty list")
                                    return []

                                # If depth is 0, return empty list (PROPFIND Depth:0 means no descendants)
                                if depth == 0:
                                    logger.debug(f"[WebDAV] VirtualResource.get_descendants: depth=0, returning empty list (no descendants)")
                                    return []

                                try:
                                    # Convert string depth to int (wsgidav sometimes passes string "1" instead of int 1)
                                    if isinstance(depth, str):
                                        try:
                                            depth = int(depth)
                                        except ValueError:
                                            depth = 999  # infinity

                                    # depth=1 returns immediate children, depth>1 returns all descendants recursively
                                    effective_depth = depth if isinstance(depth, int) and depth > 0 else 999
                                    children = self._provider.get_resource_list(self._path, depth=effective_depth, environ=self._environ)
                                    logger.debug(f"[WebDAV] VirtualResource.get_descendants: get_resource_list returned {len(children) if children else 0} children for {self._path} (depth={effective_depth})")
                                    return children if children else []
                                except Exception as e:
                                    logger.error(f"[WebDAV] VirtualResource.get_descendants error: {e}", exc_info=True)
                                    return []
                            
                            def get_property_names(self, *, is_allprop):
                                """Return list of property names in Clark notation."""
                                return [
                                    "{{DAV:}}getlastmodified",
                                    "{{DAV:}}getcontentlength",
                                    "{{DAV:}}resourcetype",
                                    "{{DAV:}}displayname",
                                    "{{DAV:}}getcontenttype",
                                ]

                            def get_property_value(self, name):
                                """Return value for a specific property (name in Clark notation)."""
                                logger.debug(f"[WebDAV] VirtualResource.get_property_value called: name={name}, path={self._path}")
                                from datetime import datetime
                                if name == "{{DAV:}}getlastmodified":
                                    ts = self._modified if hasattr(self, '_modified') else 0
                                    return datetime.fromtimestamp(ts).strftime('%a, %d %b %Y %H:%M:%S GMT') if ts else ""
                                elif name == "{{DAV:}}getcontentlength":
                                    return str(self._size) if not self._is_dir else "0"
                                elif name == "{{DAV:}}resourcetype":
                                    from wsgidav.util import etree
                                    if self._is_dir:
                                        elem = etree.Element("{{DAV:}}resourcetype")
                                        etree.SubElement(elem, "{{DAV:}}collection")
                                        return elem
                                    return ""
                                elif name == "{{DAV:}}displayname":
                                    return self._path.split('/')[-1] if hasattr(self, '_path') else ""
                                elif name == "{{DAV:}}getcontenttype":
                                    if self._is_dir:
                                        return 'httpd/unix-directory'
                                    # CRITICAL: Use get_content_type() to return proper MIME types for audio files
                                    # Flacbox needs correct MIME types (audio/mpeg, audio/mp4, etc.) to recognize playable files
                                    return self.get_content_type()
                                else:
                                    from wsgidav.dav_error import HTTP_NOT_FOUND, DAVError
                                    raise DAVError(HTTP_NOT_FOUND, f"Property {{name}} not found")


                            def get_properties(self, propname="allprop", name_list=None):
                                # Return properties as list of (name, value) tuples - wsgidav expects this format
                                from datetime import datetime
                                props = []
                                if propname == "allprop" or "getlastmodified" in str(propname):
                                    props.append(('getlastmodified', datetime.fromtimestamp(self._modified).strftime('%a, %d %b %Y %H:%M:%S GMT')))
                                if propname == "allprop" or "getcontentlength" in str(propname):
                                    props.append(('getcontentlength', str(self._size) if not self._is_dir else "0"))
                                if propname == "allprop" or "resourcetype" in str(propname):
                                    props.append(('resourcetype', '<D:collection/>' if self._is_dir else ''))
                                if propname == "allprop" or "displayname" in str(propname):
                                    props.append(('displayname', self._path.split('/')[-1] or self._path))
                                return props

                            def get_preferred_path(self):
                                """Return preferred path for this resource."""
                                if self._path in ("", "/"):
                                    return "/"
                                # Append '/' for collections
                                if self.is_collection and not self._path.endswith("/"):
                                    return self._path + "/"
                                return self._path

                            def get_directory_info(self):
                                # Return dict-like info for directory browser
                                from datetime import datetime
                                return {
                                    "display_name": self.get_display_name(),
                                    "href": self.get_href(),
                                    "is_collection": self.is_collection(),
                                    "last_modified": datetime.fromtimestamp(self._modified).strftime('%a, %d %b %Y %H:%M:%S GMT'),
                                    "content_length": self.get_content_length(),
                                }
                            
                            def __getitem__(self, key):
                                # Make it dict-like for directory browser
                                if key == "display_name":
                                    return self.get_display_name()
                                elif key == "href":
                                    return self.get_href()
                                elif key == "is_collection":
                                    return self.is_collection()
                                elif key == "last_modified":
                                    # wsgidav expects numeric timestamp, not formatted string
                                    return float(self._modified) if isinstance(self._modified, (int, float)) else 0.0
                                elif key == "str_modified":
                                    # This is set by wsgidav's directory browser after formatting
                                    if hasattr(self, '_dict_storage') and key in self._dict_storage:
                                        return self._dict_storage[key]
                                    return ""
                                elif key == "content_length":
                                    return self.get_content_length()
                                raise KeyError(key)
                            
                            def get(self, key, default=None):
                                # Dict-like get() method for directory browser compatibility
                                try:
                                    return self.__getitem__(key)
                                except KeyError:
                                    return default
                            
                            def __setitem__(self, key, value):
                                # Allow item assignment for directory browser
                                if key == "str_modified":
                                    # Directory browser sets this after formatting
                                    if not hasattr(self, '_dict_storage'):
                                        self._dict_storage = {}
                                    self._dict_storage[key] = value
                                elif key == "display_name":
                                    pass  # Ignore
                                else:
                                    # Store in a dict if needed
                                    if not hasattr(self, '_dict_storage'):
                                        self._dict_storage = {}
                                    self._dict_storage[key] = value
                            
                            def __contains__(self, key):
                                # Support 'in' operator
                                return key in ["display_name", "href", "is_collection", "last_modified", "str_modified", "content_length"] or (hasattr(self, '_dict_storage') and key in self._dict_storage)
                            
                            def get_directory_info(self):
                                # Return directory info for dir_browser - delegate to get_resource_instances
                                try:
                                    children = self._provider.get_resource_instances(self._path, environ=None)
                                    return children if children else []
                                except Exception as e:
                                    logger.error(f"[WebDAV] VirtualResource.get_directory_info error: {e}")
                                    return []
                            
                            def __getattr__(self, name):
                                # Gracefully handle any other method calls
                                logger.debug(f"[WebDAV] VirtualResource.__getattr__ called for '{name}' on path {self._path} - returning None")
                                return None

                    # Now get info from remote storage
                    info = self._proxy_get_info(username, rel_path)
                    logger.debug(f"[WebDAV] _proxy_get_info returned: info={info}, username={username}, rel_path={rel_path}")

                    if info:
                        logger.debug(f"[WebDAV] Got info from proxy: is_dir={info.get('is_directory', False)}, path={rel_path}")
                        result = VirtualResource(normalized_path, info, self, environ=environ)
                        logger.debug(f"[WebDAV] Created VirtualResource for {normalized_path}: is_collection={result.is_collection}, size={result.get_content_length()}, path={result.path}")
                        return result
                    else:
                        # File doesn't exist yet - determine if it's a directory or file request
                        # ONLY auto-create directories if path explicitly ends with /
                        # This prevents files without extensions from being created as directories

                        # Check if this is explicitly a directory request (ends with /)
                        is_directory_request = normalized_path.endswith('/') or rel_path.endswith('/')

                        if is_directory_request:
                            # Path explicitly ends with / - this is a directory
                            # Create it on the remote storage
                            logger.info(f"[WebDAV] Directory requested (ends with /): {rel_path}, creating it automatically")
                            try:
                                # Remove trailing slash for directory creation
                                dir_path = rel_path.rstrip('/')
                                self._proxy_create_directory(username, dir_path)
                                logger.info(f"[WebDAV] Successfully created directory: {dir_path}")
                                # Return a VirtualResource representing the newly created directory
                                info = {
                                    'path': normalized_path.rstrip('/'),
                                    'name': dir_path.split('/')[-1] if dir_path else '',
                                    'is_directory': True,
                                    'size': 0,
                                    'modified': time.time(),
                                }
                                result = VirtualResource(normalized_path.rstrip('/'), info, self, environ=environ)
                                logger.info(f"[WebDAV] Created VirtualResource for new directory {normalized_path}: is_collection={result.is_collection}")
                                return result
                            except Exception as e:
                                logger.error(f"[WebDAV] Failed to create directory {rel_path}: {e}")
                                return None
                        else:
                            # Path doesn't end with / - assume it's a file (even without extension)
                            # This allows PUT operations to create new files
                            # Joplin uses hash-based filenames without extensions
                            logger.info(f"[WebDAV] Creating VirtualResource for non-existent file (no extension check): {normalized_path}")
                            info = {
                                'path': normalized_path,
                                'name': rel_path.split('/')[-1] if rel_path else '',
                                'is_directory': False,
                                'size': 0,
                                'modified': time.time(),
                            }
                            result = VirtualResource(normalized_path, info, self, environ=environ)
                            logger.debug(f"[WebDAV] Created VirtualResource for new file {normalized_path}: is_collection={result.is_collection}, size={result.get_content_length()}, path={result.path}")
                            return result
                except Exception as e:
                    logger.error(f"[WebDAV] Failed to get info from proxy: {e}", exc_info=True)
                    # Don't fall back to local - raise the error
                    raise
            
            # If we can't get username, try parent class as fallback
            logger.warning(f"[WebDAV] Could not extract username from path {normalized_path}, trying parent class")
            try:
                result = super().get_resource_inst(normalized_path, environ)
                logger.debug(f"[WebDAV]  Parent get_resource_inst returned: type={type(result)}")
                # IMPORTANT: Wrap the parent's resource to ensure path is always a string
                if result:
                    result = ResourcePathWrapper(result)
                    logger.debug(f"[WebDAV] Wrapped parent resource, path={result.path}, type={type(result.path)}")
                return result
            except Exception as e:
                logger.debug(f"[WebDAV]  Parent class failed: {e}, creating minimal resource")
                # Fallback: create minimal resource with all required methods
                # Ensure time module is accessible for nested class (closure needs it in function scope)
                _ = time  # Reference time in function scope
                
                class VirtualResource(_DAVResource):
                    def __init__(self, path, is_dir=True, environ=None):
                        # Ensure path is always a string, never a list
                        path_str = str(path[0]) if isinstance(path, list) and len(path) > 0 else str(path)
                        
                        # CRITICAL: Directories MUST have trailing slashes for WebDAV compliance
                        # Many clients (like Flacbox) require this to recognize directories
                        if is_dir and path_str and not path_str.endswith('/'):
                            path_str = path_str + '/'
                            logger.debug(f"[WebDAV] Added trailing slash to directory path in fallback VirtualResource.__init__: {path_str}")
                        
                        # Manually set required attributes without calling super().__init__
                        if environ is None:
                            environ = {}

                        # Set attributes directly using object.__setattr__ to avoid any property issues
                        object.__setattr__(self, 'provider', environ.get('wsgidav.provider'))
                        object.__setattr__(self, 'path', path_str)
                        object.__setattr__(self, 'is_collection', is_dir)
                        object.__setattr__(self, 'environ', environ)
                        object.__setattr__(self, 'name', path_str.split('/')[-1] if path_str else '')
                        object.__setattr__(self, '_path', path_str)
                        object.__setattr__(self, '_is_dir', is_dir)
                        object.__setattr__(self, '_modified', time.time())

                    def get_last_modified(self):
                        return self._modified
                    
                    def get_content_length(self):
                        return 0


                    def get_display_name(self):
                        return self._path.split('/')[-1] or self._path

                    def get_ref_url(self):
                        # Return full path including /webdav/ prefix
                        ref_url = str(self._path)
                        if not ref_url.startswith('/webdav/'):
                            ref_url = '/webdav' + ref_url
                        return ref_url

                    def get_href(self):
                        """Return href (URL path) for the resource."""
                        # CRITICAL: Extract string from list if _path is somehow a list
                        if isinstance(self._path, list):
                            logger.error(f"[WebDAV] ⚠️  CRITICAL: Resource._path is a list: {self._path}")
                            href = str(self._path[0]) if len(self._path) > 0 else '/'
                        else:
                            href = str(self._path)

                        # CRITICAL: Remove any existing /webdav/ prefix to normalize
                        if href.startswith('/webdav/'):
                            href = href[7:]  # Remove '/webdav/'
                        elif href.startswith('/webdav'):
                            href = href[7:]  # Remove '/webdav'
                        
                        # CRITICAL: Remove duplicate username patterns from href
                        # Handle cases like: /username/username/path or /username/webdav/username/path
                        import re
                        path_parts = href.strip('/').split('/')
                        if len(path_parts) > 0 and '@' in path_parts[0]:
                            username = path_parts[0]
                            username_pattern = re.escape(username) + r'/'
                            # Remove duplicate username/username/ or username/webdav/username/ patterns
                            href = re.sub(r'^/' + username_pattern + r'(webdav/)?' + username_pattern, f'/{username}/', href)
                        
                        # CRITICAL: Directories MUST end with trailing slash for WebDAV compliance
                        # Many clients (like Flacbox) require this to recognize directories
                        if self.is_collection and not href.endswith('/'):
                            href = href + '/'
                            logger.debug(f"[WebDAV] Added trailing slash to directory href: {href}")

                        # CRITICAL: WSGiDAV is NOT reliably prepending SCRIPT_NAME to hrefs in XML responses
                        # We must include /webdav/ prefix explicitly to ensure consistent absolute paths
                        script_name = self.environ.get('SCRIPT_NAME', '/webdav') if hasattr(self, 'environ') and self.environ else '/webdav'
                        if script_name and not href.startswith(script_name):
                            # Ensure href starts with / (absolute path)
                            if not href.startswith('/'):
                                href = '/' + href
                            # Prepend /webdav/ to make it absolute
                            href = script_name.rstrip('/') + href
                        
                        # CRITICAL: Do NOT URL-encode @ symbol - Joplin expects @ in hrefs to match base URL
                        # The HTTP layer will handle URL encoding when needed
                        # href = href.replace('@', '%40')  # REMOVED - causes Joplin sync errors
                        
                        script_name_check = self.environ.get('SCRIPT_NAME', '') if hasattr(self, 'environ') and self.environ else ''
                        # Changed to debug to avoid logging every single file in large directories (e.g., 1900+ songs)
                        logger.debug(f"[WebDAV] Resource.get_href returning: {href} (script_name={script_name_check})")
                        return href

                    def get_preferred_path(self):
                        """Return preferred path for this resource."""
                        if self._path in ("", "/"):
                            return "/"
                        # Append '/' for collections
                        if self.is_collection and not self._path.endswith("/"):
                            return self._path + "/"
                        return self._path

                    def get_etag(self):
                        return None

                    def get_content_type(self):
                        """Return proper MIME type for the resource."""
                        if self._is_dir:
                            return 'httpd/unix-directory'
                        
                        # Get file extension from path
                        path_str = str(self._path) if hasattr(self, '_path') else ''
                        if '.' in path_str:
                            ext = path_str.rsplit('.', 1)[-1].lower()
                            # Common audio MIME types
                            audio_mime_types = {
                                'mp3': 'audio/mpeg',
                                'm4a': 'audio/mp4',
                                'm4b': 'audio/mp4',
                                'aac': 'audio/aac',
                                'flac': 'audio/flac',
                                'ogg': 'audio/ogg',
                                'oga': 'audio/ogg',
                                'wav': 'audio/wav',
                                'wma': 'audio/x-ms-wma',
                                'opus': 'audio/opus',
                            }
                            if ext in audio_mime_types:
                                return audio_mime_types[ext]
                        
                        # Default to octet-stream for unknown types
                        return 'application/octet-stream'

                    def get_content(self):
                        """Fallback VirtualResource - no content (used for directories)."""
                        return None

                    def begin_write(self, *, content_type=None):
                        """Open content for writing - return a file-like object."""
                        import io
                        logger.debug(f"[WebDAV] VirtualResource(fallback).begin_write called for {self._path}")
                        if self._is_dir:
                            from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN
                            raise DAVError(HTTP_FORBIDDEN, "Cannot write to a directory")
                        # Return a BytesIO buffer that we'll write to the storage server in end_write
                        self._write_buffer = io.BytesIO()
                        return self._write_buffer

                    def end_write(self, *, with_errors):
                        """Called after writing is complete."""
                        logger.debug(f"[WebDAV] VirtualResource(fallback).end_write called for {self._path}, with_errors={with_errors}")
                        if with_errors:
                            logger.error(f"[WebDAV] Write completed with errors for {self._path}")
                            return
                        # This is a fallback resource, likely doesn't have a provider to write to
                        # Just log and ignore
                        logger.warning(f"[WebDAV] VirtualResource(fallback) end_write - no provider to write to")

                    def support_content_length(self):
                        return False

                    def get_descendants(self, depth=1, add_self=False, depth_first=False):
                        return []

                    def get_property_names(self, *, is_allprop):
                        """Return list of property names in Clark notation."""
                        return [
                            "{{DAV:}}getlastmodified",
                            "{{DAV:}}getcontentlength",
                            "{{DAV:}}resourcetype",
                            "{{DAV:}}displayname",
                            "{{DAV:}}getcontenttype",
                        ]

                    def get_property_value(self, name):
                        """Return value for a specific property (name in Clark notation)."""
                        from datetime import datetime
                        logger.debug(f"[WebDAV] get_property_value called: name={{name}}, path={{self._path if hasattr(self, '_path') else 'unknown'}}")
                        if name == "{{DAV:}}getlastmodified":
                            ts = self._modified if hasattr(self, '_modified') else 0
                            return datetime.fromtimestamp(ts).strftime('%a, %d %b %Y %H:%M:%S GMT') if ts else ""
                        elif name == "{{DAV:}}getcontentlength":
                            return str(self._size) if not self._is_dir else "0"
                        elif name == "{{DAV:}}resourcetype":
                            from wsgidav.util import etree
                            if self._is_dir:
                                elem = etree.Element("{{DAV:}}resourcetype")
                                etree.SubElement(elem, "{{DAV:}}collection")
                                return elem
                            return ""
                        elif name == "{{DAV:}}displayname":
                            return self._path.split('/')[-1] if hasattr(self, '_path') else ""
                        elif name == "{{DAV:}}getcontenttype":
                            if self._is_dir:
                                return 'httpd/unix-directory'
                            # CRITICAL: Use get_content_type() to return proper MIME types for audio files
                            # Flacbox needs correct MIME types (audio/mpeg, audio/mp4, etc.) to recognize playable files
                            return self.get_content_type()
                        else:
                            from wsgidav.dav_error import HTTP_NOT_FOUND, DAVError
                            raise DAVError(HTTP_NOT_FOUND, f"Property {{name}} not found")


                    def get_properties(self, propname="allprop", name_list=None):
                        # Return properties as list of (name, value) tuples - wsgidav expects this format
                        from datetime import datetime
                        props = []
                        if propname == "allprop" or "getlastmodified" in str(propname):
                            props.append(('getlastmodified', datetime.fromtimestamp(self._modified).strftime('%a, %d %b %Y %H:%M:%S GMT')))
                        if propname == "allprop" or "getcontentlength" in str(propname):
                            props.append(('getcontentlength', "0"))  # Directory has no content length
                        if propname == "allprop" or "resourcetype" in str(propname):
                            props.append(('resourcetype', '<D:collection/>' if self._is_dir else ''))
                        if propname == "allprop" or "displayname" in str(propname):
                            props.append(('displayname', self._path.split('/')[-1] or self._path))
                        return props

                    def get_directory_info(self):
                        # Return directory info for dir_browser - return empty list
                        return []

                    # Add any other methods wsgidav might call
                    def __getattr__(self, name):
                        # Return None for any missing attributes to prevent AttributeError
                        logger.warning(f"[WebDAV] VirtualResource missing attribute: {name}, returning None")
                        return None
                
                logger.debug(f"[WebDAV] Creating VirtualResource for {normalized_path}")
                resource = VirtualResource(normalized_path, is_dir=True, environ=environ)
                return resource
        
        logger.debug(f"[WebDAV] Using parent class for local filesystem")
        # Use parent class for local filesystem
        try:
            result = super().get_resource_inst(normalized_path, environ)
            logger.debug(f"[WebDAV] Parent get_resource_inst returned: type={type(result)}")
            # IMPORTANT: Wrap the parent's resource to ensure path is always a string
            if result:
                result = ResourcePathWrapper(result)
                logger.debug(f"[WebDAV] Wrapped parent resource, path={result.path}, type={type(result.path)}")
            return result
        except Exception as e:
            logger.error(f"[WebDAV] get_resource_inst error from parent: {e}", exc_info=True)
            raise
    
    def get_filestream(self, path: str, environ: dict = None):
        """Override get_filestream to proxy from remote storage if configured.

        WsgiDAV calls this method to get a file-like object for streaming file content.
        We need to intercept this for remote storage and return content from the storage server.
        """
        import io
        logger.info(f"[WebDAV] 🎵 get_filestream CALLED for audio playback: path={path}")

        # Strip /webdav prefix if present
        normalized_path = path.strip('/')
        while normalized_path.startswith('webdav/'):
            normalized_path = normalized_path[7:]
        if normalized_path:
            normalized_path = '/' + normalized_path
        else:
            normalized_path = '/'

        # If remote storage is configured, proxy the download
        if self.storage_server_url:
            username = self._get_username_from_path(normalized_path)
            if username:
                # Extract relative path
                rel_path = normalized_path.lstrip('/')
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path == username:
                    rel_path = ''

                # Normalize rel_path
                rel_path = rel_path.rstrip(' /')

                try:
                    content = self._proxy_download_file(username, rel_path)
                    logger.info(f"[WebDAV] get_filestream: proxied {len(content)} bytes for {username}/{rel_path}")
                    return io.BytesIO(content)
                except Exception as e:
                    logger.error(f"[WebDAV] get_filestream proxy error: {e}")
                    raise

        # Fall back to parent for local filesystem
        return super().get_filestream(path, environ)

    def get_content(self, path: str, environ: dict = None):
        """Override get_content to proxy from remote storage if configured."""
        import io
        logger.info(f"[WebDAV] 🎵 get_content CALLED for audio playback: path={path}")

        # Strip /webdav prefix if present
        normalized_path = path.strip('/')
        while normalized_path.startswith('webdav/'):
            normalized_path = normalized_path[7:]
        if normalized_path:
            normalized_path = '/' + normalized_path
        else:
            normalized_path = '/'

        # If remote storage is configured, proxy the download
        if self.storage_server_url:
            username = self._get_username_from_path(normalized_path)
            if username:
                # Extract relative path
                rel_path = normalized_path.lstrip('/')
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path == username:
                    rel_path = ''

                # Normalize rel_path
                rel_path = rel_path.rstrip(' /')

                try:
                    content = self._proxy_download_file(username, rel_path)
                    logger.info(f"[WebDAV] get_content: proxied {len(content)} bytes for {username}/{rel_path}")
                    return io.BytesIO(content)
                except Exception as e:
                    logger.error(f"[WebDAV] get_content proxy error: {e}")
                    raise

        # Fall back to parent for local filesystem
        return super().get_content(path, environ)
    
    def _proxy_list_files(self, username: str, path: str, depth: int = 1, environ: dict = None):
        """Proxy file listing - uses the same proxying mechanism as files router.

        Args:
            username: User's username
            path: Path to list
            depth: Listing depth (1=immediate children, >1=recursive/infinity)
            environ: WSGI environ dict (used to get SCRIPT_NAME for href generation)
        """
        import requests
        # Ensure time module is accessible for nested SimpleResource class
        # (needed for closure to capture module-level import)
        _ = time  # Reference time in function scope so nested class can access it

        # Use the same proxying logic as /api/files/list
        # Call storage_server_url/api/storage/list-files with server token
        if not self.storage_server_url:
            raise Exception("storage_server_url not configured")

        # CRITICAL: Normalize depth - WebDAV depth=1 means immediate children only, not recursive
        # Some clients send depth=1 but expect only direct children, not all descendants
        # For Music directory with 2800+ files, we MUST limit to immediate children for performance
        if isinstance(depth, str):
            depth = depth.lower()
            if depth in ('infinity', 'inf'):
                depth = 999  # Use large number for infinity
            else:
                try:
                    depth = int(depth)
                except:
                    depth = 1
        elif depth is None:
            depth = 1
        
        # CRITICAL: For Music directory, force depth=1 (immediate children only) to prevent timeout
        # Even if client requests depth=infinity, limit to immediate children for large directories
        if path and 'Music' in path and depth > 1:
            logger.warning(f"[WebDAV] Limiting depth from {depth} to 1 for Music directory to prevent timeout")
            depth = 1
        
        # Check cache first (only for depth=1 to avoid caching huge recursive listings)
        cache_key = (username, path, depth)
        current_time = time.time()
        cached_items = None
        
        if depth == 1:  # Only cache immediate children listings
            with self._cache_lock:
                if cache_key in self._dir_cache:
                    cache_time, cached_items = self._dir_cache[cache_key]
                    # Use longer cache TTL for Music directory
                    cache_ttl = self._music_cache_ttl if path and 'Music' in path else self._cache_ttl
                    if current_time - cache_time < cache_ttl:
                        logger.debug(f"[WebDAV] Cache hit for {username}/{path} (depth={depth}, cache_age={current_time - cache_time:.1f}s)")
                        # Continue to process cached items into SimpleResource objects
                        items = cached_items
                    else:
                        # Cache expired
                        items = None
                        del self._dir_cache[cache_key]
                        logger.debug(f"[WebDAV] Cache expired for {username}/{path} (age={current_time - cache_time:.1f}s > {cache_ttl}s)")
                else:
                    items = None
        else:
            items = None

        # If not in cache, fetch from storage server
        try:
            if items is None:
                url = f"{self.storage_server_url.rstrip('/')}/api/storage/list-files"
                headers = {}
                if self.storage_server_token:
                    headers["Authorization"] = f"Bearer {self.storage_server_token}"
                else:
                    logger.warning(f"[WebDAV] No storage_server_token configured - authentication may fail")

                params = {
                    "username": username,
                    "path": path,
                    "depth": depth
                }

                # CRITICAL: Increase timeout for deep listings (depth > 1) which can take longer
                # Large Music directories with depth=infinity can take 60+ seconds
                # Use longer timeout for recursive listings, shorter for immediate children
                if isinstance(depth, str) and depth.lower() in ('infinity', 'inf'):
                    timeout = 120  # 2 minutes for infinity depth
                elif isinstance(depth, int) and depth > 1:
                    timeout = 90  # 90 seconds for recursive listings
                else:
                    timeout = 30  # 30 seconds for immediate children (depth=1)

                logger.debug(f"[WebDAV] Proxying to storage server: {url} for username={username}, path={path}, depth={depth}, timeout={timeout}s")
                # Add retry logic for connection errors
                max_retries = 3
                retry_delay = 2
                last_error = None
                
                for attempt in range(max_retries):
                    try:
                        response = requests.get(url, headers=headers, params=params, timeout=timeout)
                        logger.debug(f"[WebDAV] Storage server response: status={response.status_code}, content_length={len(response.content)}")
                        
                        if response.status_code != 200:
                            logger.error(f"[WebDAV] Storage server error: {response.status_code} - {response.text[:200]}")
                        
                        response.raise_for_status()
                        break  # Success, exit retry loop
                    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                        last_error = e
                        if attempt < max_retries - 1:
                            logger.warning(f"[WebDAV] Connection error (attempt {attempt + 1}/{max_retries}): {e}, retrying in {retry_delay}s...")
                            time.sleep(retry_delay)
                            retry_delay *= 2
                        else:
                            logger.error(f"[WebDAV] Failed to connect to storage server after {max_retries} attempts: {e}")
                            raise
                    except requests.exceptions.RequestException as e:
                        # Non-connection errors, don't retry
                        logger.error(f"[WebDAV] Storage server request error: {e}")
                        raise
                
                # After successful response, parse JSON
                data = response.json()
                
                # Convert File Manager format to WebDAV format
                items = data.get('items', [])
                if len(items) == 0:
                    logger.debug(f"[WebDAV] Storage server returned 0 items for {username}/{path} (depth={depth})")
                elif len(items) > 1000:
                    logger.info(f"[WebDAV] Storage server returned {len(items)} items for {username}/{path} (depth={depth}) - large listing")
                else:
                    logger.debug(f"[WebDAV] Storage server returned {len(items)} items for {username}/{path} (depth={depth})")
                
                # Cache the result if depth=1 (immediate children only)
                if depth == 1:
                    with self._cache_lock:
                        self._dir_cache[cache_key] = (current_time, items)
                        logger.debug(f"[WebDAV] Cached {len(items)} items for {username}/{path} (depth={depth})")
                        # Clean up old cache entries (keep cache size reasonable)
                        if len(self._dir_cache) > 100:
                            # Remove oldest entries
                            sorted_keys = sorted(self._dir_cache.keys(), key=lambda k: self._dir_cache[k][0])
                            for old_key in sorted_keys[:20]:
                                del self._dir_cache[old_key]
            
            # Process items into SimpleResource objects (whether from cache or server)
            # Ensure items is always a list before processing
            if items is None:
                items = []
            
            # wsgidav expects a list of ResourceInfo objects, not dictionaries
            # We need to create virtual filesystem entries and use the parent class to create proper resources
            # OR we can create DAVCollection/DAVNonCollection objects directly
            from datetime import datetime
            
            webdav_resources = []
            start_time = time.time()

            # OPTIMIZATION: Pre-compile regex patterns outside the loop
            import re
            username_pattern_cache = {}
            
            for idx, item in enumerate(items):
                # Log progress for very large listings only (every 2000 items to reduce log spam)
                if len(items) > 3000 and idx > 0 and idx % 2000 == 0:
                    elapsed = time.time() - start_time
                    rate = idx / elapsed if elapsed > 0 else 0
                    logger.info(f"[WebDAV] Processing items: {idx}/{len(items)} ({elapsed:.1f}s elapsed, ~{rate:.1f} items/sec)")
                # Use 'name' for the filename, not 'path' (which may include parent directories)
                item_name = item.get('name', '')
                if not item_name:
                    # Fallback to extracting name from path
                    item_path = item.get('path', '')
                    item_name = item_path.split('/')[-1] if item_path else ''

                # Build full WebDAV path: /username/path/item_name
                if path:
                    full_path = f"/{username}/{path}/{item_name}" if item_name else f"/{username}/{path}"
                else:
                    full_path = f"/{username}/{item_name}" if item_name else f"/{username}"

                # Normalize path (remove double slashes)
                full_path = full_path.replace('//', '/')
                
                # CRITICAL: Remove any duplicate username patterns that might have been introduced
                # Handle cases like: /username/username/path -> /username/path
                # OPTIMIZATION: Use cached pattern
                if username not in username_pattern_cache:
                    username_pattern_cache[username] = re.escape(username) + r'/'
                username_pattern = username_pattern_cache[username]
                # Remove duplicate username/username/ pattern
                full_path = re.sub(r'^/' + username_pattern + username_pattern, f'/{username}/', full_path)

                # CRITICAL: Ensure full_path is a string, not a list
                if isinstance(full_path, list):
                    logger.debug(f"[WebDAV]  full_path is a list! Converting to string: {full_path}")
                    full_path = str(full_path[0]) if len(full_path) > 0 else ''
                full_path = str(full_path)  # Force conversion to string
                
                is_directory = item.get('is_directory', False)
                
                # CRITICAL: Directories MUST end with trailing slash for WebDAV compliance
                # Many clients (like Flacbox) require this to recognize directories
                if is_directory and not full_path.endswith('/'):
                    full_path = full_path + '/'
                    # Only log for first few to reduce overhead
                    if idx < 10:
                        logger.debug(f"[WebDAV] Added trailing slash to directory path: {full_path}")
                size = item.get('size', 0) if not is_directory else 0
                modified = item.get('modified', 0)
                
                # Convert modified timestamp - wsgidav expects timestamp (float), not datetime
                # time is already imported at module level and accessible via closure
                if isinstance(modified, (int, float)):
                    modified_ts = float(modified)
                else:
                    modified_ts = time.time()
                
                # OPTIMIZATION: Skip expensive logging for large directories
                # Create a virtual resource object that wsgidav can use
                # wsgidav expects ResourceInfo objects with specific methods
                # Let's use the parent class to create proper resources by creating virtual files
                # OR we can create a custom ResourceInfo-like object
                # Actually, let's check what the parent class returns and mimic that
                # For now, let's try using the parent's _locate_file_path and creating resources through it
                # But since files don't exist locally, we need a different approach
                
                # wsgidav's FilesystemProvider.get_resource_list returns a list of ResourceInfo objects
                # These need to have methods like get_last_modified(), get_content_length(), etc.
                # The error shows wsgidav is calling get_last_modified() on items, so each item must be a resource object
                # Let's create a proper resource info object that matches what wsgidav expects
                
                # Create a simple resource object with all required methods
                class SimpleResource(_DAVResource):
                    def __init__(self, path, is_dir, size, modified, provider=None, environ=None):
                        # Ensure path is always a string, never a list
                        path_str = str(path[0]) if isinstance(path, list) and len(path) > 0 else str(path)
                        
                        # CRITICAL: Remove duplicate username patterns from path
                        # Handle cases like: /username/webdav/username/path or /username/username/path
                        import re
                        # Extract username from path if possible (first component after /)
                        path_parts = path_str.strip('/').split('/')
                        if len(path_parts) > 0 and '@' in path_parts[0]:
                            username = path_parts[0]
                            username_pattern = re.escape(username) + r'/'
                            # Remove duplicate username/username/ or username/webdav/username/ patterns
                            path_str = re.sub(r'^/' + username_pattern + r'(webdav/)?' + username_pattern, f'/{username}/', path_str)
                        
                        # CRITICAL: Directories MUST have trailing slashes for WebDAV compliance
                        # Many clients (like Flacbox) require this to recognize directories
                        # Note: full_path already has trailing slash if is_directory, but ensure it here too
                        if is_dir and path_str and not path_str.endswith('/'):
                            path_str = path_str + '/'
                            logger.debug(f"[WebDAV] Added trailing slash to directory path in SimpleResource.__init__: {path_str}")
                        
                        # Manually set required attributes without calling super().__init__
                        if environ is None:
                            environ = {"wsgidav.provider": provider} if provider else {}

                        # Set attributes directly using object.__setattr__ to avoid any property issues
                        object.__setattr__(self, 'provider', provider)
                        object.__setattr__(self, 'path', path_str)
                        object.__setattr__(self, 'is_collection', is_dir)
                        object.__setattr__(self, 'environ', environ)
                        object.__setattr__(self, 'name', path_str.split('/')[-1] if path_str else '')
                        object.__setattr__(self, '_path', path_str)
                        object.__setattr__(self, '_is_dir', is_dir)
                        object.__setattr__(self, '_size', size)
                        object.__setattr__(self, '_modified', modified)
                        object.__setattr__(self, '_provider', provider)

                    def __getattribute__(self, name):
                        """Override to ensure path is always a string."""
                        if name == 'path':
                            _path = object.__getattribute__(self, '_path')
                            result = str(_path)
                            return result
                        return object.__getattribute__(self, name)

                    def get_last_modified(self):
                        # Return timestamp as float (not datetime)
                        return float(self._modified) if isinstance(self._modified, (int, float)) else time.time()
                    
                    def get_content_length(self):
                        return self._size if not self._is_dir else 0

                    def is_collection(self):
                        """Return True if this is a collection (directory)."""
                        return self._is_dir

                    def get_display_name(self):
                        return self._path.split('/')[-1] or self._path

                    def get_ref_url(self):
                        # Return full path including /webdav/ prefix
                        ref_url = str(self._path)
                        if not ref_url.startswith('/webdav/'):
                            ref_url = '/webdav' + ref_url
                        return ref_url

                    def get_href(self):
                        """Return href (URL path) for the resource."""
                        # CRITICAL: Extract string from list if _path is somehow a list
                        if isinstance(self._path, list):
                            logger.error(f"[WebDAV] ⚠️  CRITICAL: Resource._path is a list: {self._path}")
                            href = str(self._path[0]) if len(self._path) > 0 else '/'
                        else:
                            href = str(self._path)

                        # CRITICAL: Remove any /webdav/ prefix - wsgidav will prepend SCRIPT_NAME automatically
                        # If we include /webdav/ here, wsgidav might prepend it again, causing duplicates
                        if href.startswith('/webdav/'):
                            href = href[7:]  # Remove '/webdav/'
                        elif href.startswith('/webdav'):
                            href = href[7:]  # Remove '/webdav'
                        
                        # CRITICAL: Remove duplicate username patterns from href
                        # Handle cases like: /username/username/path or /username/webdav/username/path
                        import re
                        path_parts = href.strip('/').split('/')
                        if len(path_parts) > 0 and '@' in path_parts[0]:
                            username = path_parts[0]
                            username_pattern = re.escape(username) + r'/'
                            # Remove duplicate username/username/ or username/webdav/username/ patterns
                            href = re.sub(r'^/' + username_pattern + r'(webdav/)?' + username_pattern, f'/{username}/', href)
                        
                        # CRITICAL: Directories MUST end with trailing slash for WebDAV compliance
                        # Many clients (like Flacbox) require this to recognize directories
                        if self.is_collection and not href.endswith('/'):
                            href = href + '/'
                            logger.debug(f"[WebDAV] Added trailing slash to directory href: {href}")

                        # CRITICAL: WSGiDAV is NOT prepending SCRIPT_NAME to hrefs in XML responses
                        # The final XML shows hrefs without /webdav/ prefix, which breaks Flacbox GET requests
                        # We must include /webdav/ prefix explicitly so Flacbox can construct correct GET URLs
                        script_name = self.environ.get('SCRIPT_NAME', '/webdav') if hasattr(self, 'environ') and self.environ else '/webdav'
                        if script_name and not href.startswith(script_name):
                            # Prepend script_name to ensure hrefs in XML include /webdav/ prefix
                            href = script_name.rstrip('/') + href
                        
                        # CRITICAL: Do NOT URL-encode @ symbol - Joplin expects @ in hrefs to match base URL
                        # The HTTP layer will handle URL encoding when needed
                        # href = href.replace('@', '%40')  # REMOVED - causes Joplin sync errors
                        
                        script_name_check = self.environ.get('SCRIPT_NAME', '') if hasattr(self, 'environ') and self.environ else ''
                        # Log hrefs for audio files to debug Flacbox playback issues
                        if any(ext in href.lower() for ext in ['.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac']):
                            logger.info(f"[WebDAV] 🎵 SimpleResource.get_href: Audio file href={href} (script_name={script_name_check}, path={self._path})")
                        else:
                            logger.debug(f"[WebDAV] Resource.get_href returning: {href} (script_name={script_name_check})")
                        return href

                    def get_preferred_path(self):
                        """Return preferred path for this resource."""
                        if self._path in ("", "/"):
                            return "/"
                        # Append '/' for collections
                        if self.is_collection and not self._path.endswith("/"):
                            return self._path + "/"
                        return self._path

                    def get_etag(self):
                        # Return None or empty string if no ETag
                        return None

                    def get_content_type(self):
                        """Return proper MIME type for the resource."""
                        if self._is_dir:
                            return 'httpd/unix-directory'
                        
                        # Get file extension from path
                        path_str = str(self._path)
                        if '.' in path_str:
                            ext = path_str.rsplit('.', 1)[-1].lower()
                            # Common audio MIME types
                            audio_mime_types = {
                                'mp3': 'audio/mpeg',
                                'm4a': 'audio/mp4',
                                'm4b': 'audio/mp4',
                                'aac': 'audio/aac',
                                'flac': 'audio/flac',
                                'ogg': 'audio/ogg',
                                'oga': 'audio/ogg',
                                'wav': 'audio/wav',
                                'wma': 'audio/x-ms-wma',
                                'opus': 'audio/opus',
                            }
                            if ext in audio_mime_types:
                                return audio_mime_types[ext]
                        
                        # Default to octet-stream for unknown types
                        return 'application/octet-stream'

                    def get_content(self):
                        """Return file content as file-like object from remote storage.

                        WsgiDAV expects get_content() to return a file-like object (not bytes)
                        that supports read() method.
                        """
                        import io
                        from wsgidav.dav_error import DAVError, HTTP_INTERNAL_ERROR
                        if self._is_dir or not self._provider:
                            return None
                        # Storage server expects full email-style username (e.g., verita84@poster.place)
                        path = self._path.strip('/')
                        if path.startswith('webdav/'):
                            path = path[7:]
                        parts = path.split('/', 1)
                        if len(parts) >= 2:
                            username, rel_path = parts[0], parts[1]
                        elif len(parts) == 1:
                            username, rel_path = parts[0], ''
                        else:
                            return None
                        try:
                            content = self._provider._proxy_download_file(username, rel_path)
                            logger.info(f"[WebDAV] SimpleResource.get_content: downloaded {len(content)} bytes for {rel_path}")
                            # Return file-like object, not raw bytes
                            return io.BytesIO(content)
                        except Exception as e:
                            logger.error(f"[WebDAV] SimpleResource.get_content error: {e}")
                            # Raise DAVError instead of returning None to avoid AttributeError on close()
                            raise DAVError(HTTP_INTERNAL_ERROR, f"Failed to retrieve file content: {e}")

                    def support_content_length(self):
                        return not self._is_dir

                    def begin_write(self, *, content_type=None):
                        """Open content for writing - return a file-like object."""
                        import io
                        logger.debug(f"[WebDAV] SimpleResource.begin_write called for {self._path}")
                        if self._is_dir:
                            from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN
                            raise DAVError(HTTP_FORBIDDEN, "Cannot write to a directory")

                        # Create a custom buffer that captures content before close()
                        class CaptureBuffer(io.BytesIO):
                            def __init__(self, resource):
                                super().__init__()
                                self._resource = resource
                                self._captured_content = None

                            def close(self):
                                # Capture content before closing
                                if not self._captured_content:
                                    self._captured_content = self.getvalue()
                                    self._resource._write_content = self._captured_content
                                super().close()

                        self._write_buffer = CaptureBuffer(self)
                        return self._write_buffer

                    def end_write(self, *, with_errors):
                        """Called after writing is complete."""
                        logger.debug(f"[WebDAV] SimpleResource.end_write called for {self._path}, with_errors={with_errors}")
                        if with_errors:
                            logger.error(f"[WebDAV] Write completed with errors for {self._path}")
                            return
                        # Get the captured content (stored by close() in CaptureBuffer)
                        if hasattr(self, '_write_content'):
                            content = self._write_content
                            logger.debug(f"[WebDAV] Writing {len(content)} bytes to {self._path}")
                            try:
                                if self._provider:
                                    self._provider.write_file_content(self._path, content)
                                    logger.info(f"[WebDAV] Successfully wrote {len(content)} bytes to {self._path}")
                                else:
                                    logger.error(f"[WebDAV] No provider available to write to {self._path}")
                                    from wsgidav.dav_error import DAVError, HTTP_INTERNAL_ERROR
                                    raise DAVError(HTTP_INTERNAL_ERROR, "No storage provider available")
                            except Exception as e:
                                logger.error(f"[WebDAV] Failed to write to {self._path}: {e}", exc_info=True)
                                from wsgidav.dav_error import DAVError, HTTP_INTERNAL_ERROR
                                raise DAVError(HTTP_INTERNAL_ERROR, f"Failed to write file: {e}")
                            finally:
                                if hasattr(self, '_write_content'):
                                    delattr(self, '_write_content')
                                if hasattr(self, '_write_buffer'):
                                    delattr(self, '_write_buffer')
                        else:
                            logger.warning(f"[WebDAV] end_write called but no content captured for {self._path}")

                    def get_descendants(self, depth=1, add_self=False, depth_first=False):
                        # Return empty list or None - wsgidav will call get_resource_instances for children
                        return []

                    def get_property_names(self, *, is_allprop):
                        """Return list of property names in Clark notation."""
                        props = [
                            "{DAV:}getlastmodified",
                            "{DAV:}getcontentlength",
                            "{DAV:}displayname",
                            "{DAV:}getcontenttype",
                        ]
                        # Only include resourcetype for directories - Flacbox prefers files without it
                        if self._is_dir:
                            props.append("{DAV:}resourcetype")
                        return props

                    def get_property_value(self, name):
                        """Return value for a specific property (name in Clark notation)."""
                        from datetime import datetime
                        prop_name = name  # Store for logging
                        logger.debug(f"[WebDAV] SimpleResource.get_property_value: name={prop_name}, path={self._path if hasattr(self, '_path') else 'unknown'}, is_dir={self._is_dir}")
                        if name == "{DAV:}getlastmodified":
                            ts = self._modified if hasattr(self, '_modified') else 0
                            return datetime.fromtimestamp(ts).strftime('%a, %d %b %Y %H:%M:%S GMT') if ts else ""
                        elif name == "{DAV:}getcontentlength":
                            return str(self._size) if not self._is_dir else "0"
                        elif name == "{DAV:}resourcetype":
                            from wsgidav.util import etree
                            if self._is_dir:
                                # CRITICAL: Return proper XML element for collection - Flacbox requires this
                                elem = etree.Element("{DAV:}resourcetype")
                                etree.SubElement(elem, "{DAV:}collection")
                                # Log for directories to help debug Flacbox issues
                                logger.info(f"[WebDAV] Returning resourcetype with collection for directory: {self._path}, is_collection={self.is_collection()}")
                                return elem
                            # For files, return None instead of empty element - some clients (like Flacbox) prefer this
                            # Empty element might confuse clients that expect either collection or nothing
                            return None
                        elif name == "{DAV:}displayname":
                            return self._path.split('/')[-1] if hasattr(self, '_path') else ""
                        elif name == "{DAV:}getcontenttype":
                            # Use get_content_type() method which has proper MIME type detection
                            return self.get_content_type()
                        else:
                            from wsgidav.dav_error import HTTP_NOT_FOUND, DAVError
                            raise DAVError(HTTP_NOT_FOUND, f"Property {prop_name} not found")


                    def get_properties(self, propname="allprop", name_list=None):
                        # Return properties as list of (name, value) tuples - wsgidav expects this format
                        from datetime import datetime
                        props = []
                        if propname == "allprop" or "getlastmodified" in str(propname):
                            props.append(('getlastmodified', datetime.fromtimestamp(self._modified).strftime('%a, %d %b %Y %H:%M:%S GMT')))
                        if propname == "allprop" or "getcontentlength" in str(propname):
                            props.append(('getcontentlength', str(self._size) if not self._is_dir else "0"))
                        if propname == "allprop" or "resourcetype" in str(propname):
                            # CRITICAL: Return proper XML element for resourcetype (not string)
                            # wsgidav expects XML elements for resourcetype in get_properties
                            from wsgidav.util import etree
                            if self._is_dir:
                                elem = etree.Element("{DAV:}resourcetype")
                                etree.SubElement(elem, "{DAV:}collection")
                                props.append(('resourcetype', elem))
                                logger.debug(f"[WebDAV] get_properties: Added resourcetype element for directory: {self._path}")
                            else:
                                # For files, don't include resourcetype at all (return None)
                                # Some clients like Flacbox prefer files without resourcetype rather than empty element
                                # Don't append resourcetype for files
                                pass
                        if propname == "allprop" or "displayname" in str(propname):
                            props.append(('displayname', self._path.split('/')[-1] or self._path))
                        if propname == "allprop" or "getcontenttype" in str(propname):
                            # Use get_content_type() method which has proper MIME type detection
                            props.append(('getcontenttype', self.get_content_type()))
                        return props
                    
                    def get_directory_info(self):
                        # Return directory info as dict for dir_browser
                        from datetime import datetime
                        return {
                            "display_name": self.get_display_name(),
                            "href": self.get_href(),
                            "is_collection": self.is_collection(),
                            "content_length": self.get_content_length(),
                            "last_modified": datetime.fromtimestamp(self._modified).strftime('%a, %d %b %Y %H:%M:%S GMT'),
                        }
                    
                    # Make it fully dict-like for directory browser - SINGLE VERSION
                    def __getitem__(self, key):
                        # Support all keys the directory browser might use
                        from datetime import datetime
                        if key == "display_name":
                            return self.get_display_name()
                        elif key == "href":
                            return self.get_href()
                        elif key == "is_collection":
                            return self.is_collection
                        elif key == "is_directory":
                            return self._is_dir
                        elif key == "path":
                            return self._path
                        elif key == "size":
                            return self._size if not self._is_dir else 0
                        elif key == "modified":
                            # Return numeric timestamp (float/int)
                            return float(self._modified) if isinstance(self._modified, (int, float)) else 0.0
                        elif key == "last_modified":
                            # wsgidav expects numeric timestamp, not formatted string
                            return float(self._modified) if isinstance(self._modified, (int, float)) else 0.0
                        elif key == "str_modified":
                            # This is set by wsgidav's directory browser after formatting
                            # Return empty string initially, wsgidav will set it
                            if hasattr(self, '_dict_storage') and key in self._dict_storage:
                                return self._dict_storage[key]
                            return ""
                        elif key == "content_length":
                            return self._size if not self._is_dir else 0
                        else:
                            # Check if it's in dict_storage (for keys set by directory browser)
                            if hasattr(self, '_dict_storage') and key in self._dict_storage:
                                return self._dict_storage[key]
                            raise KeyError(f"'{key}' not found in SimpleResource")
                    
                    def __setitem__(self, key, value):
                        # Allow item assignment for directory browser
                        if key == "str_modified":
                            # Directory browser sets this, just ignore it
                            pass
                        elif key == "display_name":
                            # Can't really change this, ignore
                            pass
                        else:
                            # For other keys, store in a dict-like storage
                            if not hasattr(self, '_dict_storage'):
                                self._dict_storage = {}
                            self._dict_storage[key] = value
                    
                    def get(self, key, default=None):
                        # Dict-like get() method
                        try:
                            return self[key]
                        except KeyError:
                            return default
                    
                    def __contains__(self, key):
                        # Support 'in' operator
                        return key in ["display_name", "href", "is_collection", "is_directory", "path", "size", "modified", "last_modified", "str_modified", "content_length"] or (hasattr(self, '_dict_storage') and key in self._dict_storage)
                    
                    def get_directory_info(self):
                        # Return directory info as dict for dir_browser
                        from datetime import datetime
                        return {
                            "display_name": self.get_display_name(),
                            "href": self.get_href(),
                            "is_collection": self.is_collection(),
                            "is_directory": self._is_dir,
                            "content_length": self.get_content_length(),
                            "last_modified": datetime.fromtimestamp(self._modified).strftime('%a, %d %b %Y %H:%M:%S GMT') if isinstance(self._modified, (int, float)) else "",
                        }
                
                # OPTIMIZATION: Skip expensive logging for large directories
                # Only log first few resources to reduce overhead
                if idx < 5:
                    logger.debug(f"[WebDAV] Creating SimpleResource: full_path={full_path}, type={type(full_path)}")
                resource = SimpleResource(full_path, is_directory, size, modified_ts, provider=self, environ=environ)
                if idx < 5:
                    logger.debug(f"[WebDAV] Created resource.path={resource.path}, type={type(resource.path)}, script_name={environ.get('SCRIPT_NAME', '') if environ else 'NO_ENVIRON'}")
                # Note: Don't call get_href() here - it will be called by wsgidav when building response
                webdav_resources.append(resource)

                # NOTE: Server-side recursive listing disabled - too slow with one API call per directory
                # The sync client handles recursive listing more efficiently
                # if depth > 1 and is_directory:
                #     ... recursive listing code ...

            elapsed_total = time.time() - start_time
            # Count directories vs files for debugging
            dir_count = sum(1 for r in webdav_resources if hasattr(r, '_is_dir') and r._is_dir)
            file_count = len(webdav_resources) - dir_count
            logger.info(f"[WebDAV] Created {len(webdav_resources)} SimpleResource objects (depth={depth}) in {elapsed_total:.2f}s: {dir_count} directories, {file_count} files")

            # Deduplicate resources by path to prevent multiple <d:href> elements for same resource
            # Use resource.path directly instead of calling get_href() to avoid duplicate href calculations
            seen_paths = set()
            unique_resources = []
            duplicate_count = 0
            for res in webdav_resources:
                res_path = str(res.path) if hasattr(res, 'path') else str(res)
                if res_path not in seen_paths:
                    seen_paths.add(res_path)
                    unique_resources.append(res)
                else:
                    duplicate_count += 1
                    logger.warning(f"[WebDAV] ⚠️  Removed duplicate resource: {res_path}")

            if duplicate_count > 0:
                logger.error(f"[WebDAV] ⚠️  CRITICAL: Removed {duplicate_count} duplicate resources! ({len(webdav_resources)} -> {len(unique_resources)})")

            elapsed_final = time.time() - start_time
            if elapsed_final > 10:
                logger.warning(f"[WebDAV] Slow directory listing: took {elapsed_final:.2f}s for {len(unique_resources)} items - consider using depth=1 for large directories")
            
            # Log summary for debugging Flacbox issues
            dir_count = sum(1 for r in unique_resources if hasattr(r, '_is_dir') and r._is_dir)
            file_count = len(unique_resources) - dir_count
            logger.info(f"[WebDAV] Returning {len(unique_resources)} unique resources: {dir_count} directories, {file_count} files (duplicates removed: {duplicate_count})")
            
            # OPTIMIZATION: Skip expensive href collection for large directories
            # Only collect sample hrefs for smaller directories or first few items
            if unique_resources and len(unique_resources) <= 100:
                sample_hrefs = []
                audio_hrefs = []
                for res in unique_resources[:10]:
                    try:
                        href = res.get_href() if hasattr(res, 'get_href') else str(res.path)
                        sample_hrefs.append(href)
                        # Collect audio file hrefs specifically
                        if any(ext in href.lower() for ext in ['.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac']):
                            audio_hrefs.append(href)
                    except Exception as e:
                        sample_hrefs.append(str(res.path) if hasattr(res, 'path') else 'unknown')
                logger.debug(f"[WebDAV] Sample hrefs (first 10): {sample_hrefs}")
                if audio_hrefs:
                    logger.info(f"[WebDAV] ⚠️  Audio file hrefs in PROPFIND response (first 5): {audio_hrefs[:5]}")
            elif unique_resources and len(unique_resources) > 100:
                # For large directories, just log count
                logger.debug(f"[WebDAV] Large directory: {len(unique_resources)} resources (skipping href collection for performance)")

            return unique_resources
            
        except Exception as e:
            logger.error(f"[WebDAV] Failed to proxy list to storage server: {e}", exc_info=True)
            raise
    
    def get_resource_info(self, path: str, environ: dict = None):
        """Override to ensure correct resource type detection, with remote storage support."""
        # Strip /webdav prefix if present
        path_stripped = path.strip('/')
        if path_stripped.startswith('webdav/'):
            path_stripped = '/' + path_stripped[7:]
        else:
            path_stripped = path
        # Normalize path - remove trailing slash and spaces for files
        normalized_path = path_stripped.rstrip('/ ')
        
        # If remote storage is configured, ALWAYS proxy - never use local filesystem
        if self.storage_server_url:
            username = self._get_username_from_path(normalized_path)
            if username:
                # Extract relative path
                rel_path = normalized_path.lstrip('/')
                
                # Handle various path formats (same as in get_resource_inst)
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path.startswith('webdav/' + username + '/'):
                    rel_path = rel_path[len('webdav/' + username) + 1:]
                elif rel_path.startswith('calendar/dav/' + username + '/'):
                    rel_path = rel_path[len('calendar/dav/' + username) + 1:]
                elif rel_path.startswith('calendar/' + username + '/'):
                    rel_path = rel_path[len('calendar/' + username) + 1:]
                elif rel_path == username or rel_path == f'webdav/{username}' or rel_path == f'calendar/dav/{username}' or rel_path == f'calendar/{username}':
                    rel_path = ''
                else:
                    # Try to find username in path
                    username_pos = rel_path.find(username)
                    if username_pos != -1:
                        after_username = rel_path[username_pos + len(username):].lstrip('/')
                        rel_path = after_username
                    else:
                        rel_path = ''
                
                # Normalize rel_path - trim trailing spaces
                rel_path = rel_path.rstrip(' /')
                
                # Get info from storage server - this is the ONLY way when remote storage is configured
                try:
                    info = self._proxy_get_info(username, rel_path)
                    if info:
                        logger.debug(f"[WebDAV] Got resource info from storage server for {normalized_path}")
                        return info
                    # If not found, log and return None (404)
                    logger.debug(f"[WebDAV] Resource not found in storage server: {normalized_path} (rel_path: {rel_path})")
                    return None
                except Exception as e:
                    logger.error(f"[WebDAV] Failed to get info from storage server: {e}")
                    # Don't fall back to local - raise the error
                    raise
        
        # Always check filesystem directly first for accurate info
        fs_path = self._locate_file_path(normalized_path)
        if fs_path and fs_path.exists():
            # Get info directly from filesystem - most accurate
            stat = fs_path.stat()
            info = {
                'iscollection': fs_path.is_dir(),
                'size': 0 if fs_path.is_dir() else stat.st_size,
                'modified': stat.st_mtime,
            }
            logger.debug(f"[WebDAV] Resource info from filesystem for {normalized_path}: isdir={fs_path.is_dir()}, size={info['size']}")
            return info
        
        # If filesystem path doesn't exist, try parent method
        info = super().get_resource_info(normalized_path, environ)
        
        if info:
            # Double-check against filesystem if possible
            if fs_path and fs_path.exists():
                if fs_path.is_file() and info.get('iscollection', False):
                    # File is incorrectly reported as collection - fix it
                    info['iscollection'] = False
                    info['size'] = fs_path.stat().st_size
                    logger.debug(f"[WebDAV] Fixed resource type for {normalized_path}: file, not directory")
                elif fs_path.is_dir() and not info.get('iscollection', False):
                    # Directory is incorrectly reported as file - fix it
                    info['iscollection'] = True
                    info['size'] = 0
                    logger.debug(f"[WebDAV] Fixed resource type for {normalized_path}: directory, not file")
        
        return info
    
    def _proxy_get_info(self, username: str, path: str):
        """Get file info - calls remote storage server directly with caching."""
        import requests

        # Normalize path - trim trailing spaces and slashes
        path = path.rstrip(' /')

        # Remove duplicate webdav/username prefix if present
        # Example: "webdav/user@domain" -> ""
        if path == f'webdav/{username}':
            path = ''
            logger.debug(f"[WebDAV] _proxy_get_info: removed duplicate webdav/{username}, path is now empty")
        elif path.startswith(f'webdav/{username}/'):
            path = path[len(f'webdav/{username}/'):]
            logger.debug(f"[WebDAV] _proxy_get_info: removed duplicate webdav/{username}/, path={path}")

        # If path equals username, treat as root directory
        if path == username:
            path = ''
            logger.debug(f"[WebDAV] _proxy_get_info: path equals username, treating as root directory")

        # For root directory (empty path), return directory info
        if not path:
            # Root directory always exists - return directory info
            return {
                'path': f"/{username}",
                'name': username,
                'is_directory': True,
                'size': 0,
                'modified': 0,
            }

        # Get parent directory and filename
        path_parts = path.split('/')
        parent_path = '/'.join(path_parts[:-1]) if len(path_parts) > 1 else ''
        filename = path_parts[-1].rstrip()  # Trim trailing spaces from filename

        # Check cache first
        cache_key = (username, parent_path)
        current_time = time.time()

        with self._cache_lock:
            if cache_key in self._dir_cache:
                cache_time, cached_items = self._dir_cache[cache_key]
                if current_time - cache_time < self._cache_ttl:
                    # Cache is still valid
                    items = cached_items
                    logger.debug(f"[WebDAV] Cache hit for {parent_path} ({len(items)} items)")
                else:
                    # Cache expired
                    items = None
                    del self._dir_cache[cache_key]
            else:
                items = None

        # If not in cache, fetch from storage server
        if items is None:
            url = f"{self.storage_server_url.rstrip('/')}/api/storage/list-files"
            headers = {}
            if self.storage_server_token:
                headers["Authorization"] = f"Bearer {self.storage_server_token}"

            # Ensure path is explicitly empty string (not None) for root directory
            params = {
                "username": username,
                "path": parent_path if parent_path else ""
            }
            
            logger.debug(f"[WebDAV] _proxy_get_info: calling storage API with username={username}, path='{params['path']}' (parent_path was '{parent_path}')")

            try:
                response = requests.get(url, headers=headers, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()
                items = data.get('items', [])

                # Store in cache
                with self._cache_lock:
                    self._dir_cache[cache_key] = (current_time, items)
                    # Clean up old cache entries (keep cache size reasonable)
                    if len(self._dir_cache) > 100:
                        # Remove oldest entries
                        sorted_keys = sorted(self._dir_cache.keys(), key=lambda k: self._dir_cache[k][0])
                        for old_key in sorted_keys[:20]:
                            del self._dir_cache[old_key]

                logger.debug(f"[WebDAV] Fetched directory listing for {parent_path} ({len(items)} items)")
            except Exception as e:
                logger.error(f"[WebDAV] Failed to get info from storage server: {e}", exc_info=True)
                return None

        # Find the file in the listing
        matched_item = None

        # First try exact match
        for item in items:
            item_name = item.get('name', '')
            if item_name == filename:
                matched_item = item
                break

        # If no exact match, try trimmed match (handle trailing spaces)
        if not matched_item:
            filename_trimmed = filename.rstrip()
            for item in items:
                item_name = item.get('name', '').rstrip()
                if item_name == filename_trimmed:
                    matched_item = item
                    logger.debug(f"[WebDAV] Matched file with trimmed name: '{item.get('name')}' == '{filename_trimmed}'")
                    break

        if matched_item:
            # Found it - convert to WebDAV format
            normalized_path = path.rstrip()
            full_path = f"/{username}/{normalized_path}"
            return {
                'path': full_path,
                'name': matched_item.get('name', filename).rstrip(),
                'is_directory': matched_item.get('is_directory', False),
                'size': matched_item.get('size', 0) if not matched_item.get('is_directory', False) else 0,
                'modified': matched_item.get('modified', 0),
            }
        else:
            # File not found
            logger.debug(f"[WebDAV] File not found: {filename} in {parent_path}")

        return None
    
    def read_file_content(self, path: str):
        """Override to proxy file downloads from remote storage if configured."""
        # Strip /webdav prefix if present
        path_stripped = path.strip('/')
        if path_stripped.startswith('webdav/'):
            path_stripped = '/' + path_stripped[7:]
        else:
            path_stripped = path
        # Normalize path - trim trailing spaces
        normalized_path = path_stripped.rstrip(' /')
        
        # If remote storage is configured, try to proxy the download
        if self.storage_server_url:
            username = self._get_username_from_path(normalized_path)
            if username:
                # Extract relative path
                rel_path = normalized_path.lstrip('/')
                
                # Handle various path formats (same as in get_resource_inst)
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path.startswith('webdav/' + username + '/'):
                    rel_path = rel_path[len('webdav/' + username) + 1:]
                elif rel_path.startswith('calendar/dav/' + username + '/'):
                    rel_path = rel_path[len('calendar/dav/' + username) + 1:]
                elif rel_path.startswith('calendar/' + username + '/'):
                    rel_path = rel_path[len('calendar/' + username) + 1:]
                elif rel_path == username or rel_path == f'webdav/{username}' or rel_path == f'calendar/dav/{username}' or rel_path == f'calendar/{username}':
                    rel_path = ''
                else:
                    # Try to find username in path
                    username_pos = rel_path.find(username)
                    if username_pos != -1:
                        after_username = rel_path[username_pos + len(username):].lstrip('/')
                        rel_path = after_username
                    else:
                        rel_path = ''
                
                # Normalize rel_path - trim trailing spaces
                rel_path = rel_path.rstrip(' /')
                
                # Try to proxy download
                try:
                    content = self._proxy_download_file(username, rel_path)
                    logger.debug(f"[WebDAV] Proxied file download from storage server: {normalized_path}")
                    return content
                except Exception as e:
                    logger.debug(f"[WebDAV] Failed to proxy download: {e}, trying local")
                    # Fall through to local read
        
        # Use parent method to read local file
        return super().read_file_content(normalized_path)
    
    def _proxy_download_file(self, username: str, file_path: str) -> bytes:
        """Proxy file download - calls remote storage server directly."""
        import requests

        # Normalize file_path - trim trailing spaces
        file_path = file_path.rstrip(' /')

        # Call the REMOTE storage server directly
        url = f"{self.storage_server_url}/api/storage/view-file"
        headers = {}
        if self.storage_server_token:
            headers["Authorization"] = f"Bearer {self.storage_server_token}"

        params = {
            'username': username,
            'file_path': file_path
        }

        logger.debug(f"[WebDAV] _proxy_download_file: {url} username={username} file_path={file_path}")
        response = requests.get(url, headers=headers, params=params, timeout=60, stream=True)
        response.raise_for_status()
        return response.content
    
    def delete(self, path: str):
        """Override to invalidate cache on delete."""
        # Strip /webdav prefix if present
        path_stripped = path.strip('/')
        if path_stripped.startswith('webdav/'):
            path_stripped = '/' + path_stripped[7:]
        else:
            path_stripped = path
        normalized_path = path_stripped
        
        username = self._get_username_from_path(normalized_path)
        result = super().delete(normalized_path)
        
        # Invalidate file cache for parent directory
        if username:
            self._invalidate_cache_for_path(username, normalized_path)
        
        return result
    
    def move(self, src_path: str, dst_path: str):
        """Override to invalidate cache on move."""
        # Strip /webdav prefix if present
        def normalize(p):
            p_stripped = p.strip('/')
            if p_stripped.startswith('webdav/'):
                return '/' + p_stripped[7:]
            return p
        
        normalized_src = normalize(src_path)
        normalized_dst = normalize(dst_path)
        
        username = self._get_username_from_path(normalized_src)
        result = super().move(normalized_src, normalized_dst)
        
        # Invalidate cache for both source and destination directories
        if username:
            self._invalidate_cache_for_path(username, normalized_src)
            self._invalidate_cache_for_path(username, normalized_dst)
        
        return result
    
    def _invalidate_cache_for_path(self, username: str, path: str):
        """Invalidate file cache and directory listing cache for a given path."""
        try:
            from app.routers.files import get_file_cache

            # Extract parent directory path relative to user root
            # Path format: /username/subdir/file.txt -> subdir
            # WebDAV paths are absolute, so we need to extract relative path
            path_parts = path.strip('/').split('/')
            if len(path_parts) > 1:
                # Remove username (first part) and filename (last part if file)
                # If it's a directory, include it; if it's a file, get its parent
                if len(path_parts) > 2:
                    # File: /username/subdir/file.txt -> subdir
                    parent_parts = path_parts[1:-1]
                else:
                    # Directory or file in root: /username/file.txt or /username/subdir
                    parent_parts = path_parts[1:-1] if len(path_parts) == 2 and '.' in path_parts[-1] else path_parts[1:]
                parent_path = '/'.join(parent_parts) if parent_parts else ""
            else:
                parent_path = ""

            # Normalize path (remove trailing slashes)
            parent_path = parent_path.strip('/')

            # Invalidate the file cache (from app.routers.files)
            cache = get_file_cache(self.db)
            if parent_path:
                cache.invalidate(f"{username}:{parent_path}")
            cache.invalidate(f"{username}:")  # Also invalidate root to be safe

            # Also invalidate the directory listing cache
            self._invalidate_cache(username, parent_path)

            logger.debug(f"[WebDAV] Invalidated all caches for {username}:{parent_path}")
        except Exception as e:
            logger.warning(f"[WebDAV] Failed to invalidate cache: {e}")


class PosterchanaiDomainController:
    """Domain controller for WebDAV authentication."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_domain_realm(self, path_info: str, environ: dict) -> str:
        """Return the realm for the given path."""
        return "Posterchanai WebDAV"
    
    def require_authentication(self, realm: str, environ: dict) -> bool:
        """Return True if authentication is required."""
        return True
    
    def is_authenticated(self, realm: str, username: str, password: str, environ: dict) -> bool:
        """Check if username/password is valid."""
        user = self.db.query(User).filter(User.username == username).first()
        if not user:
            return False
        
        return verify_password(password, user.password_hash)
    
    def get_realm(self, path_info: str, environ: dict) -> str:
        """Return the realm."""
        return "Posterchanai WebDAV"


def create_webdav_app(db: Session, mount_path: str = "/") -> WsgiDAVApp:
    """Create WebDAV WSGI application.
    
    Args:
        db: Database session
        mount_path: The path where WebDAV is mounted (e.g., "/webdav").
                    When mounted at /webdav, FastAPI should strip this prefix,
                    but WSGI middleware might not, so we handle it in the provider.
    """
    storage = get_storage_service(db)
    
    # Check if storage is on a remote server
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            # Files are on remote storage server - WebDAV can't access them directly
            # We need to either proxy or mount the remote storage
            # This warning is outdated - we now proxy to remote storage
            logger.info(f"[WebDAV] Storage is on remote server ({url}) - WebDAV will proxy all operations to it")
    
    root_path = Path(storage.upload_path)
    root_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"[WebDAV] Using storage path: {root_path}")
    
    # Verify the path exists and log what's in it
    if root_path.exists():
        try:
            # Count files in storage to verify it's the right location
            # Only scan first level to avoid long delays
            total_files = 0
            total_size = 0
            user_dirs = []
            
            # Scan user directories
            for item in root_path.iterdir():
                if item.is_dir():
                    user_dirs.append(item.name)
                    # Count files in this user directory (limit depth to avoid timeout)
                    try:
                        user_file_count = 0
                        user_file_size = 0
                        # Use rglob but limit to reasonable depth
                        for file_item in item.rglob('*'):
                            if file_item.is_file():
                                user_file_count += 1
                                total_files += 1
                                try:
                                    size = file_item.stat().st_size
                                    user_file_size += size
                                    total_size += size
                                except:
                                    pass
                        if user_file_count > 0:
                            logger.info(f"[WebDAV] User '{item.name}': {user_file_count} files ({user_file_size / (1024**3):.2f} GB)")
                    except Exception as e:
                        logger.debug(f"[WebDAV] Could not scan {item.name}: {e}")
                elif item.is_file():
                    total_files += 1
                    try:
                        total_size += item.stat().st_size
                    except:
                        pass
            
            logger.info(f"[WebDAV] Storage path: {root_path}")
            logger.info(f"[WebDAV] Found {len(user_dirs)} user directories")
            logger.info(f"[WebDAV] Total files: {total_files} ({total_size / (1024**3):.2f} GB)")
            
            # Check verita84 specifically
            verita84_path = root_path / 'verita84'
            if verita84_path.exists():
                verita84_files = sum(1 for _ in verita84_path.rglob('*') if _.is_file())
                verita84_size = sum(f.stat().st_size for f in verita84_path.rglob('*') if f.is_file())
                logger.info(f"[WebDAV] User 'verita84': {verita84_files} files ({verita84_size / (1024**3):.2f} GB)")
            else:
                logger.warning(f"[WebDAV] User 'verita84' directory does not exist at {verita84_path}")
        except Exception as e:
            logger.warning(f"[WebDAV] Could not scan storage path: {e}")
            import traceback
            logger.debug(f"[WebDAV] Traceback: {traceback.format_exc()}")
    else:
        logger.warning(f"[WebDAV] Storage path does not exist: {root_path}")
    
    provider = QuotaFilesystemProvider(root_path, db)
    logger.debug(f"[WebDAV]  Created QuotaFilesystemProvider: {provider}, root_path={root_path}")
    logger.debug(f"[WebDAV]  Provider type: {type(provider)}, has get_resource_list: {hasattr(provider, 'get_resource_list')}")
    
    # Use simple_dc for authentication - it accepts all users
    # We'll handle authentication at the FastAPI level via middleware
    config = {
        "provider_mapping": {
            "/": provider,  # Handle all paths from root (after mount prefix is stripped)
        },
        "simple_dc": {
            "user_mapping": {
                "*": True,  # Accept all users (authentication handled by FastAPI)
            }
        },
        "verbose": 3,  # Increase verbosity to see what wsgidav is doing
        "hotfixes": {
            "emulate_win32_lastmod": False,
        },
        # CRITICAL: Disable directory browser - it returns HTML instead of file content
        # This was causing all file downloads to get HTML pages instead of actual files
        "dir_browser": {
            "enable": False,
        },
        # Also configure HTTP authentication
        "http_authenticator": {
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
    }
    
    logger.debug(f"[WebDAV]  Creating WsgiDAVApp with provider_mapping: {list(config['provider_mapping'].keys())}")
    app = WsgiDAVApp(config)
    logger.debug(f"[WebDAV]  WsgiDAVApp created: {app}")
    return app


def start_webdav_server(db: Session, port: int = 8080) -> bool:
    """Start the WebDAV server in a background thread."""
    global _webdav_server, _webdav_thread
    
    if _webdav_server is not None:
        logger.warning("WebDAV server already running")
        return False
    
    try:
        app = create_webdav_app(db)
        
        # Create WSGI server
        _webdav_server = WSGIServer(
            bind_addr=('0.0.0.0', port),
            wsgi_app=app,
            numthreads=10
        )
        
        def run_server():
            try:
                logger.info(f"[WebDAV] Starting server on port {port}")
                _webdav_server.start()
            except Exception as e:
                logger.error(f"[WebDAV] Server error: {e}", exc_info=True)
        
        _webdav_thread = threading.Thread(target=run_server, daemon=True)
        _webdav_thread.start()
        
        logger.info(f"[WebDAV] Server started on port {port}")
        return True
    except Exception as e:
        logger.error(f"[WebDAV] Failed to start server: {e}", exc_info=True)
        return False


def stop_webdav_server():
    """Stop the WebDAV server."""
    global _webdav_server, _webdav_thread
    
    if _webdav_server is None:
        return
    
    try:
        _webdav_server.stop()
        _webdav_server = None
        if _webdav_thread:
            _webdav_thread.join(timeout=5)
            _webdav_thread = None
        logger.info("[WebDAV] Server stopped")
    except Exception as e:
        logger.error(f"[WebDAV] Error stopping server: {e}", exc_info=True)


def is_webdav_running() -> bool:
    """Check if WebDAV server is running."""
    return _webdav_server is not None and _webdav_server.ready
