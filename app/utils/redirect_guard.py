"""Every hop of a redirect is checked against the SSRF guard, not only the first URL (security review #5).

`/client/proxy-image`, `/client/xdc`, `/client/lnurl` and `/client/nip05` validated the URL they were given
and then fetched with `follow_redirects=True` -- so a public URL answering `302 http://127.0.0.1:9997/...`
(MediaMTX's unauthenticated API, whose config holds the stream-hook secret) or `302 http://nas.lan/...` was
followed straight into this node's own network and the body handed back to whoever asked. httpx calls a
REQUEST hook for every request it sends, redirects included, so one hook closes all of them:

    httpx.AsyncClient(..., follow_redirects=True, event_hooks=redirect_guard.HOOKS)

The check is `rss_service.is_safe_host` (resolves the name; private, loopback, link-local, CGNAT and
`.lan`/`.local` are refused), run off the event loop. A refused hop raises `RedirectRefused`, an
`httpx.RequestError`, so a caller's existing "could not fetch" handling answers it.
"""
import asyncio

import httpx


class RedirectRefused(httpx.RequestError):
    pass


async def _check(request: httpx.Request) -> None:
    from app.services.rss_service import is_safe_host
    url = str(request.url)
    if request.url.scheme not in ("http", "https") or not await asyncio.to_thread(is_safe_host, url):
        raise RedirectRefused(f"refused: {request.url.host} is not a public address", request=request)


HOOKS = {"request": [_check]}
