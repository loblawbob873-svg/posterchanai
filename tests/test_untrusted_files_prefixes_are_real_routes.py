"""Every route that serves bytes somebody else chose is sandboxed -- and the list that says so names REAL routes.

`UntrustedFilesMiddleware` puts `sandbox` CSP + nosniff on routes listed by PREFIX. The list said
`/api/chat/proxy-image`; the chat image proxy is mounted at `/api/proxy-image`, so a fetched SVG came back from this
origin with no sandbox at all, and nothing could notice, because a prefix that matches nothing is not an error
(found 2026-10-24). So: every prefix must match a mounted route, the routes known to serve foreign bytes must be
covered, and an SVG through the proxy must actually arrive sandboxed.
"""
import asyncio
from types import SimpleNamespace

import httpx

from app.middleware.untrusted_files import UNTRUSTED_PREFIXES


def _paths():
    """Every route path the app's routers declare (prefix + path), read from the routers themselves: FastAPI wraps
    included routers, so `app.routes` does not list them flat."""
    import importlib
    import pkgutil
    import app.routers as pkg
    from fastapi import APIRouter
    import app.main  # noqa: F401  (imports every router the app mounts)
    out = []
    for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        try:
            m = importlib.import_module(mod.name)
        except Exception:
            continue
        for obj in vars(m).values():
            if isinstance(obj, APIRouter):
                for r in obj.routes:
                    path = getattr(r, "path", "")
                    if path:
                        out.append(path if path.startswith(obj.prefix or "/~") else (obj.prefix or "") + path)
    return out


def test_every_untrusted_prefix_matches_a_mounted_route():
    paths = _paths()
    dead = [p for p in UNTRUSTED_PREFIXES if not any(x.startswith(p) or p.startswith(x.rstrip("/") + "/") for x in paths)]
    assert not dead, ("these UNTRUSTED_PREFIXES match no route -- a typo leaves the real route unsandboxed", dead)


def test_the_routes_that_serve_foreign_bytes_are_covered():
    must = ["/client/proxy-image", "/api/proxy-image", "/api/proxy-image/{thumb_id}", "/api/auth/avatar/{username}",
            "/api/mail/dl/{account_hint}/{folder}/{uid}/{idx}"]
    paths = set(_paths())
    for route in must:
        assert route in paths, ("the route moved; re-check its sandbox", route)
        assert route.startswith(UNTRUSTED_PREFIXES), ("serves bytes somebody else chose, unsandboxed", route)


def test_an_svg_through_the_chat_image_proxy_arrives_sandboxed(monkeypatch):
    from app.main import app
    from app.auth import get_current_user
    from app.database import get_db
    from app.routers import chat
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.cookie)</script></svg>'

    async def fetched(url):
        return svg, "image/svg+xml"
    monkeypatch.setattr(chat, "_proxy_fetch", fetched)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, username="m", is_admin=False)
    app.dependency_overrides[get_db] = lambda: None
    try:
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://node") as c:
                return await c.get("/api/proxy-image", params={"url": "https://elsewhere.example/x.svg"})
        r = asyncio.run(go())
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml"), r.status_code
    assert "sandbox" in r.headers.get("content-security-policy", ""), dict(r.headers)
    assert r.headers.get("x-content-type-options") == "nosniff"
