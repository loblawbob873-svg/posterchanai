"""
CSRF Protection Middleware for FastAPI

Implements Double Submit Cookie pattern:
1. A CSRF token is stored in a cookie
2. State-changing requests must include the token in a header
3. Server validates that header token matches cookie token
"""
import secrets
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from typing import Callable, Set

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
                    # Validate CSRF token
                    csrf_header = request.headers.get(CSRF_HEADER_NAME)

                    if not csrf_cookie:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="CSRF token missing from cookies"
                        )

                    if not csrf_header:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="CSRF token missing from header"
                        )

                    if not secrets.compare_digest(csrf_cookie, csrf_header):
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="CSRF token mismatch"
                        )

        # Process the request
        response = await call_next(request)

        # Set CSRF cookie if not present (for GET requests primarily)
        if not csrf_cookie:
            new_token = generate_csrf_token()
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=new_token,
                httponly=False,  # JS needs to read it for the header
                samesite="strict",
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
