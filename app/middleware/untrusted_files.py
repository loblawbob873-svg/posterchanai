"""Keep uploaded documents from executing with the application's origin."""

from starlette.datastructures import MutableHeaders


class UntrustedFilesMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith(
                ("/api/files/", "/api/storage/", "/blossom/")):
            return await self.app(scope, receive, send)

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                mime = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime in {"text/html", "application/xhtml+xml", "image/svg+xml"}:
                    headers["Content-Security-Policy"] = (
                        "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
                        "img-src data: https: http:; media-src data: blob: https: http:"
                    )
                    headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, guarded_send)
