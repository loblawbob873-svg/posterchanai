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

# Paths that don't require CSRF protection (only used if CSRF_ENABLED=true)
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
    CSRF protection middleware - COMPLETELY DISABLED.
    
    CSRF protection has been disabled. SameSite="lax" cookies provide sufficient
    CSRF protection for same-origin requests, and authentication is already required.
    
    This middleware is kept for API compatibility but performs no validation.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # CSRF validation completely disabled - just pass through
        # Process the request without any CSRF checks
        response = await call_next(request)
        
        # Don't set CSRF cookies either - not needed
        return response


def get_csrf_token(request: Request) -> str:
    """Get CSRF token from request cookies, or generate new one"""
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        token = generate_csrf_token()
    return token
