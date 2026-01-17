"""
CSRF Protection Middleware for FastAPI - DISABLED

CSRF protection has been completely disabled.
SameSite="lax" cookies provide sufficient CSRF protection for same-origin requests.
Authentication is handled via JWT tokens in cookies.
"""
import logging
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from typing import Callable

logger = logging.getLogger(__name__)

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    CSRF protection middleware - DISABLED.
    
    CSRF protection has been completely disabled. SameSite="lax" cookies provide sufficient
    CSRF protection for same-origin requests. Authentication is handled via JWT tokens.
    
    This middleware is kept for compatibility but performs no validation.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # CSRF validation is completely disabled
        # Just process the request without any CSRF checks
        response = await call_next(request)
        return response


def get_csrf_token(request: Request) -> str:
    """Get CSRF token from request cookies, or return empty string (compatibility function)"""
    return request.cookies.get(CSRF_COOKIE_NAME, "")
