"""Keep uploaded documents from executing with the application's origin."""

from starlette.datastructures import MutableHeaders


# Every route that serves bytes somebody ELSE chose -- an upload, a fetched remote image, an email
# attachment -- from this origin. A document served here runs with the app's origin, where the session and
# the user's key live, so an SVG with a <script> fetched through the image proxy (no login needed) was a
# one-click account takeover (security review, 2026-10-14). New routes of that kind belong in this list.
UNTRUSTED_PREFIXES = (
    "/api/files/", "/api/storage/", "/blossom/",
    "/client/proxy-image", "/api/chat/proxy-image",      # remote images fetched for the client/chat
    "/api/websearch/asset",                              # page assets re-served by the Web Search reader
    "/api/mail/dl/", "/api/mail/attachment/",            # attachments, typed by whoever sent the mail
    "/client/xdc",                                       # a remote .xdc fetched for the player
)
ACTIVE_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml"}


class UntrustedFilesMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith(UNTRUSTED_PREFIXES):
            return await self.app(scope, receive, send)

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                mime = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime in ACTIVE_TYPES:
                    headers["Content-Security-Policy"] = (
                        "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
                        "img-src data: https: http:; media-src data: blob: https: http:"
                    )
                    headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, guarded_send)
