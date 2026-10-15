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


def _own_media(host: str) -> bool:
    """THIS deployment's own media hosts pass, exactly as they pass `search_service.is_safe_url`.

    Split-horizon DNS sends media.poster.place to 192.168.0.1 from inside the LAN, so `is_safe_host` refuses it
    as private -- and because this hook sees the FIRST request too, not only redirects, every guarded fetch of our
    own media failed after #5 shipped: sharing a timeline image from the APK ("couldn't share image", it goes
    through /client/proxy-image), and every other guarded fetch of a media.poster.place URL. The exemption is the
    same single list (`own_media_hosts`: the configured Blossom bases + Admin -> Blossom "Own media hosts"), by
    EXACT hostname, so a redirect into the LAN by any other name is still refused. A settings read that fails
    exempts nothing."""
    try:
        from app.services.search_service import own_media_hosts
        return bool(host) and host.lower() in own_media_hosts()
    except Exception:
        return False


async def _check(request: httpx.Request) -> None:
    from app.services.rss_service import is_safe_host
    url = str(request.url)
    if request.url.scheme in ("http", "https") and _own_media(request.url.host):
        return
    if request.url.scheme not in ("http", "https") or not await asyncio.to_thread(is_safe_host, url):
        raise RedirectRefused(f"refused: {request.url.host} is not a public address", request=request)


HOOKS = {"request": [_check]}
