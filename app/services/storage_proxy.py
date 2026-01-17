"""
Storage Proxy Service - Proxy storage requests to remote storage server.
Similar to torrent proxy, but for file storage operations.
"""
import logging
import httpx
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from starlette.requests import Request
from typing import Union
from sqlalchemy.orm import Session
from app.models import Setting

logger = logging.getLogger(__name__)


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
        endpoint: API endpoint path (e.g., "/api/notes/files/user/1/file.pdf")
        method: HTTP method (GET, POST, etc.)
        json_body: JSON body for POST/PUT requests
        files: Dict of file uploads (e.g., {"file": (filename, content, content_type)})
        stream: Whether to stream the response (for file downloads)
    
    Returns:
        Response from remote server or raises HTTPException
    """
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if not storage_server_url or not storage_server_url.value:
        raise HTTPException(status_code=500, detail="Storage server not configured")
    
    # Validate that storage_server_url has a protocol
    base_url = storage_server_url.value.strip()
    if not base_url.startswith(('http://', 'https://')):
        logger.error(f"[STORAGE] Invalid storage_server_url (missing protocol): {base_url}")
        raise HTTPException(
            status_code=500, 
            detail="Storage proxy error: Request URL is missing an 'http://' or 'https://' protocol. Please configure storage_server_url with a valid URL (e.g., https://storage.example.com)"
        )
    
    # Get server-to-server API token (optional)
    storage_server_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
    
    # Also try to forward the user's cookie (fallback)
    access_token = request.cookies.get("access_token", "")
    
    # Forward Authorization header from original request (for API key auth)
    auth_header = request.headers.get("Authorization", "")
    
    url = f"{base_url.rstrip('/')}{endpoint}"
    headers = {}
    
    # Prefer server token for server-to-server auth
    if storage_server_token and storage_server_token.value:
        headers["Authorization"] = f"Bearer {storage_server_token.value}"
    elif auth_header:
        # Forward the Authorization header from the original request
        headers["Authorization"] = auth_header
    elif access_token:
        headers["Cookie"] = f"access_token={access_token}"
    
    # Forward other headers that might be needed
    if "accept" in request.headers:
        headers["Accept"] = request.headers["accept"]
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
    
    try:
        # Configure httpx client with proper connection settings
        async with httpx.AsyncClient(
            timeout=60.0,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            http2=False  # Disable HTTP/2 to avoid connection issues
        ) as client:
            # Determine auth method for logging
            if storage_server_token and storage_server_token.value:
                auth_method = 'server-token'
            elif auth_header:
                auth_method = 'forwarded-api-key'
            elif access_token:
                auth_method = 'cookie'
            else:
                auth_method = 'none'
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
                except:
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
