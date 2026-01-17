"""
CSRF Protection Middleware for FastAPI

Implements Double Submit Cookie pattern:
1. A CSRF token is stored in a cookie
2. State-changing requests must include the token in a header
3. Server validates that header token matches cookie token
"""
import secrets
import logging
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from typing import Callable, Set

logger = logging.getLogger(__name__)

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_TOKEN_LENGTH = 32

# Paths that don't require CSRF protection
CSRF_EXEMPT_PATHS: Set[str] = {
    "/v1/",      # OpenAI-compatible API uses API key auth
    "/api/tts",  # API key authenticated
    "/api/generate-image",  # Image API for load balancing (API key/JWT auth)
    "/api/storage/",  # Storage server endpoints (server-to-server auth)
    "/mcp/",     # MCP server endpoints
    "/ws/",      # WebSocket connections
    # Temporarily exempt notes folders endpoint until CSRF header issue is resolved
    # "/api/notes/folders",  # TODO: Re-enable after fixing CSRF header sending
}

# Paths that are always exempt (login, public resources, read-only endpoints)
ALWAYS_EXEMPT_PATHS: Set[str] = {
    "/login",
    "/auth/login",
    "/auth/register",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/avatar/",  # Avatar serving (read-only, may be proxied from storage server)
    "/static/",
    "/sw.js",
    "/manifest.json",
    "/favicon.ico",
}


def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token"""
    return secrets.token_hex(CSRF_TOKEN_LENGTH)


def is_path_exempt(path: str) -> bool:
    """Check if path is exempt from CSRF protection"""
    # Always exempt paths
    for exempt in ALWAYS_EXEMPT_PATHS:
        if path.startswith(exempt) or path == exempt.rstrip("/"):
            return True

    # API paths exempt (use API key auth instead)
    for exempt in CSRF_EXEMPT_PATHS:
        if path.startswith(exempt):
            return True

    return False


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    CSRF protection middleware using Double Submit Cookie pattern.

    For state-changing requests (POST, PUT, DELETE, PATCH):
    - Requires X-CSRF-Token header matching the csrf_token cookie
    - Exempt paths don't require validation

    For all responses:
    - Sets/refreshes CSRF token cookie if not present
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Get or generate CSRF token
        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)

        # Check if this is a state-changing request
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            # Skip validation for exempt paths
            if not is_path_exempt(request.url.path):
                # Skip if request has Authorization header (API key or Bearer token for server-to-server)
                auth_header = request.headers.get("Authorization")
                if not auth_header:
                    # Validate CSRF token - check both case variations and common variations
                    # HTTP headers are case-insensitive, but Starlette normalizes them to lowercase
                    # So we need to check lowercase version
                    # Starlette/ASGI normalizes header names to lowercase, so check that first
                    csrf_header = (
                        request.headers.get("x-csrf-token") or  # Lowercase (Starlette normalized)
                        request.headers.get(CSRF_HEADER_NAME) or  # Original case
                        request.headers.get(CSRF_HEADER_NAME.lower()) or  # Explicit lowercase
                        request.headers.get("X-Csrf-Token")  # Mixed case
                    )
                    
                    # Log all headers for debugging
                    all_headers = dict(request.headers)
                    logger.warning(f"CSRF check for {request.method} {request.url.path}: cookie={bool(csrf_cookie)}, header={bool(csrf_header)}, header_keys={list(all_headers.keys())}, looking_for={CSRF_HEADER_NAME}")

                    if not csrf_cookie:
                        # Log for debugging
                        logger.warning(f"CSRF token missing from cookies for {request.method} {request.url.path}")
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="CSRF token missing from cookies"
                        )

                    if not csrf_header:
                        # Log for debugging - show all headers to help diagnose
                        all_header_names = list(request.headers.keys())
                        all_headers_dict = dict(request.headers)
                        logger.error(f"CSRF token missing from header for {request.method} {request.url.path}")
                        logger.error(f"  Cookie present: {bool(csrf_cookie)}")
                        logger.error(f"  All header names: {all_header_names}")
                        logger.error(f"  All headers: {all_headers_dict}")
                        logger.error(f"  Looking for: {CSRF_HEADER_NAME}")
                        # Print to stderr as well for immediate visibility
                        import sys
                        print(f"ERROR: CSRF token missing from header for {request.method} {request.url.path}", file=sys.stderr)
                        print(f"  Headers received: {all_header_names}", file=sys.stderr)
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="CSRF token missing from header"
                        )

                    if not secrets.compare_digest(csrf_cookie, csrf_header):
                        logger.warning(f"CSRF token mismatch for {request.method} {request.url.path}")
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="CSRF token mismatch"
                        )

        # Process the request
        response = await call_next(request)

        # Set CSRF cookie if not present (for all requests to ensure cookie is always available)
        if not csrf_cookie:
            new_token = generate_csrf_token()
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=new_token,
                httponly=False,  # JS needs to read it for the header
                samesite="lax",  # Changed from "strict" to "lax" for better compatibility
                secure=request.url.scheme == "https",
                max_age=86400 * 7,  # 7 days
                path="/"
            )

        return response


def get_csrf_token(request: Request) -> str:
    """Get CSRF token from request cookies, or generate new one"""
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        token = generate_csrf_token()
    return token
