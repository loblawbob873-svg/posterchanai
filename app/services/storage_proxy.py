"""
Storage Proxy Service - Proxy storage requests to remote storage server.
Similar to torrent proxy, but for file storage operations.
"""
import logging
from app.utils import lb_auth
import re
import httpx
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from starlette.requests import Request
from typing import Union
from sqlalchemy.orm import Session
from app.services import settings_store

logger = logging.getLogger(__name__)


def sanitize_url_path(path: str) -> str:
    """Remove emojis and invalid URL characters from path"""
    if not path:
        return path
    # Remove emojis and other non-ASCII characters
    # Keep only ASCII printable characters, forward slashes, and URL-encoded sequences (%XX)
    # First, preserve URL-encoded sequences
    parts = []
    i = 0
    while i < len(path):
        if path[i] == '%' and i + 2 < len(path) and path[i+1:i+3].isalnum():
            # Preserve URL-encoded sequences
            parts.append(path[i:i+3])
            i += 3
        elif ord(path[i]) < 128 and (path[i].isprintable() or path[i] == '/'):
            # Keep ASCII printable characters and forward slashes
            parts.append(path[i])
            i += 1
        else:
            # Skip emojis and other non-ASCII characters
            i += 1
    sanitized = ''.join(parts)
    return sanitized.strip()


async def proxy_storage_request(
    db: Session,
    request: Request,
    endpoint: str,
    method: str = "GET",
    json_body: dict = None,
    files: dict = None,
    stream: bool = False
) -> Union[Response, StreamingResponse, dict]:
    """
    Forward storage request to remote storage server.
    
    Args:
        db: Database session
        request: Original FastAPI request
        endpoint: API endpoint path (e.g., "/api/storage/files/user/1/file.pdf")
        method: HTTP method (GET, POST, etc.)
        json_body: JSON body for POST/PUT requests
        files: Dict of file uploads (e.g., {"file": (filename, content, content_type)})
        stream: Whether to stream the response (for file downloads)
    
    Returns:
        Response from remote server or raises HTTPException
    """
    storage_server_url = settings_store.get("storage_server_url")
    if not storage_server_url:
        raise HTTPException(status_code=500, detail="Storage server not configured")

    # Validate that storage_server_url has a protocol
    base_url = storage_server_url.strip()
    if not base_url.startswith(('http://', 'https://')):
        logger.error(f"[STORAGE] Invalid storage_server_url (missing protocol): {base_url}")
        raise HTTPException(
            status_code=500, 
            detail="Storage proxy error: Request URL is missing an 'http://' or 'https://' protocol. Please configure storage_server_url with a valid URL (e.g., https://storage.example.com)"
        )
    
    # Sanitize endpoint to remove emojis and invalid characters
    sanitized_endpoint = sanitize_url_path(endpoint)
    if not sanitized_endpoint:
        logger.warning(f"[STORAGE] Invalid endpoint after sanitization (empty result): {endpoint}")
        raise HTTPException(status_code=400, detail="Invalid endpoint path")
    if sanitized_endpoint != endpoint:
        logger.warning(f"[STORAGE] Sanitized endpoint (removed invalid characters): {endpoint} -> {sanitized_endpoint}")
    
    # Ensure endpoint starts with / if it's a relative path
    if not sanitized_endpoint.startswith('/'):
        sanitized_endpoint = '/' + sanitized_endpoint
    
    url = f"{base_url.rstrip('/')}{sanitized_endpoint}"
    # Server-to-server requests don't need authentication - use load-balanced header
    headers = lb_auth.headers()
    
    # Forward other headers that might be needed
    if "accept" in request.headers:
        headers["Accept"] = request.headers["accept"]
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
    
    try:
        # Configure httpx client with proper connection settings
        # Use explicit IP address resolution and disable HTTP/2
        transport = httpx.AsyncHTTPTransport(
            retries=3,
            http2=False
        )
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            transport=transport
        ) as client:
            # Determine auth method for logging
            auth_method = 'load-balanced'
            logger.info(f"[STORAGE] Proxying {method} {url} (auth: {auth_method})")
            
            if method == "GET":
                response = await client.get(url, headers=headers, follow_redirects=True)
                logger.debug(f"[STORAGE] GET response status: {response.status_code} for {url}")
            elif method == "POST":
                if files:
                    # Handle file uploads (multipart/form-data)
                    files_data = {}
                    for key, (filename, content, content_type) in files.items():
                        files_data[key] = (filename, content, content_type)
                    # json_body contains form data when files are present
                    form_data = json_body or {}
                    response = await client.post(url, headers=headers, files=files_data, data=form_data, follow_redirects=True)
                else:
                    # Regular JSON POST
                    response = await client.post(url, headers=headers, json=json_body, follow_redirects=True)
            elif method == "PUT":
                if files:
                    # Handle file uploads (multipart/form-data)
                    files_data = {}
                    for key, (filename, content, content_type) in files.items():
                        files_data[key] = (filename, content, content_type)
                    response = await client.put(url, headers=headers, files=files_data, data=json_body or {}, follow_redirects=True)
                else:
                    # Regular JSON PUT
                    response = await client.put(url, headers=headers, json=json_body, follow_redirects=True)
            elif method == "DELETE":
                response = await client.delete(url, headers=headers, follow_redirects=True)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")
            
            logger.info(f"[STORAGE] Remote response: {response.status_code}")
            
            # Handle file streaming responses (for file downloads)
            if stream and response.status_code == 200:
                # Get content type from response
                content_type = response.headers.get("content-type", "application/octet-stream")
                
                response_headers = {}
                if "content-length" in response.headers:
                    response_headers["Content-Length"] = response.headers["content-length"]
                if "content-disposition" in response.headers:
                    response_headers["Content-Disposition"] = response.headers["content-disposition"]
                if "content-type" in response.headers:
                    response_headers["Content-Type"] = response.headers["content-type"]
                
                # For HEAD requests, return headers only (no body)
                if method == "HEAD":
                    from fastapi.responses import Response
                    return Response(
                        status_code=response.status_code,
                        headers=response_headers
                    )
                
                # Create streaming response for GET requests
                async def generate():
                    async for chunk in response.aiter_bytes():
                        yield chunk
                
                return StreamingResponse(
                    generate(),
                    status_code=response.status_code,
                    media_type=content_type,
                    headers=response_headers
                )
            
            # Handle JSON responses (for non-streaming requests)
            if not stream and response.status_code == 200:
                try:
                    return response.json()
                except Exception as e:
                    logger.error(f"[STORAGE] Failed to parse JSON response: {e}, body: {response.text[:500]}")
                    raise HTTPException(status_code=500, detail="Invalid response from storage server")
            
            # For streaming responses that aren't 200, or non-streaming errors
            if response.status_code != 200:
                try:
                    error_detail = response.json().get("detail", response.text)
                except Exception:
                    error_detail = response.text or f"HTTP {response.status_code}"
                
                raise HTTPException(status_code=response.status_code, detail=error_detail)
            
            # If we get here with stream=True and 200, we already returned StreamingResponse above
            # This shouldn't happen, but handle it gracefully
            raise HTTPException(status_code=500, detail="Unexpected response from storage server")
            
    except httpx.TimeoutException:
        logger.error("[STORAGE] Timeout connecting to storage server")
        raise HTTPException(status_code=504, detail="Storage server timeout")
    except httpx.ConnectError as e:
        logger.error(f"[STORAGE] Cannot connect to storage server: {e}")
        logger.error(f"[STORAGE] Attempted URL: {url}")
        logger.error(f"[STORAGE] Error details: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=503, detail=f"Cannot reach storage server: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[STORAGE] Error proxying request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Storage proxy error: {str(e)}")
