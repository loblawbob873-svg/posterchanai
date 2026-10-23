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
import ipaddress
import socket

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


class PinnedTransport(httpx.AsyncHTTPTransport):
    """Connects to the address the guard JUDGED, not to whatever the name resolves to a moment later.

    The hook above resolves a name and refuses private answers; httpx then resolved it AGAIN to connect, so a name
    whose DNS answers public-then-private (rebinding, TTL 0) passed the check and reached this node's own network
    (security review 2026-10-18, `/client/thumb`). Here the name is resolved once, every answer is judged by the
    hook's own rules, and the connection is made to that IP with the name kept for TLS (SNI, certificate) and the
    Host header. This deployment's own media hosts (`_own_media`) are connected to as resolved -- split-horizon DNS
    is exactly why they are exempt. Use it WITH the hooks, which still judge every redirect hop's scheme and name:

        httpx.AsyncClient(transport=redirect_guard.PinnedTransport(), event_hooks=redirect_guard.HOOKS, ...)
    """

    def __init__(self, *args, trust=None, **kwargs):
        """`trust(host) -> bool`: a caller's OWN exemptions (names its guard already passes as this deployment's
        own, e.g. `search_service`'s trusted domains), connected to as resolved exactly like `_own_media`."""
        super().__init__(*args, **kwargs)
        self._trust = trust

    async def handle_async_request(self, request):
        host = request.url.host
        if _own_media(host) or (self._trust is not None and host and self._trust(host)):
            return await super().handle_async_request(request)
        if not host or host.lower() == "localhost" or host.lower().endswith((".local", ".internal", ".lan")):
            raise RedirectRefused(f"refused: {host} is not a public address", request=request)
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        except OSError as e:
            raise httpx.ConnectError(f"cannot resolve {host}", request=request) from e
        addrs = []
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                    or ip.is_unspecified or not ip.is_global:
                raise RedirectRefused(f"refused: {host} resolves to a private address", request=request)
            addrs.append(ip)
        if not addrs:
            raise httpx.ConnectError(f"cannot resolve {host}", request=request)
        request.url = request.url.copy_with(host=str(addrs[0]))
        if request.url.scheme == "https":
            request.extensions = {**request.extensions, "sni_hostname": host}
        return await super().handle_async_request(request)
