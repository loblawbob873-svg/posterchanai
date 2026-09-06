"""Origin checks for cookie-authenticated mutations, including native-app cookies."""
from urllib.parse import urlsplit
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from app.auth import NATIVE_APP_ORIGINS


class CookieOriginMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'http' and scope.get('method') not in {'GET', 'HEAD', 'OPTIONS'}:
            headers = Headers(scope=scope)
            cookie = headers.get('cookie', '')
            origin = headers.get('origin')
            # Bearer/Nostr API requests must prove their own credentials, independently
            # of browser ambient authority. SameSite remains defense in depth.
            authorization = headers.get('authorization', '').split(None, 1)
            explicit_bearer = (len(authorization) == 2 and authorization[0].lower() == 'bearer'
                               and bool(authorization[1].strip()))
            if 'access_token=' in cookie and origin and not explicit_bearer:
                allowed = origin.rstrip('/') in NATIVE_APP_ORIGINS
                try:
                    parsed = urlsplit(origin)
                    proto = headers.get('x-forwarded-proto', scope.get('scheme', 'http')).split(',')[0].strip()
                    allowed = allowed or (parsed.scheme == proto and parsed.netloc == headers.get('host')
                                          and not parsed.path and not parsed.query and not parsed.fragment)
                except ValueError:
                    pass
                if not allowed:
                    return await JSONResponse({'detail': 'Cross-origin cookie request denied'},
                                              status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)
