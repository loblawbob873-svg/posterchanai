"""The redirect guard lets THIS node's own media hosts through, like the SSRF check it sits beside.

Reported 2026-10-15 from the APK: timeline image -> Share -> "couldn't share image". The APK shares through
/client/proxy-image, and every image on poster.place's own Blossom (media.poster.place) answered 502: split-horizon
DNS resolves it to 192.168.0.1, and redirect_guard -- added for security #5, and run on the FIRST request as well as
every redirect -- refused it as private, although `is_safe_url` exempts our own media hosts. Measured live:
media.poster.place 502 through the proxy, 200 direct; image.nostr.build 200 through the proxy.

The shipped hook is driven with a host that resolves PRIVATE: our own media host passes, the same address by any
other name is still refused, and an own-hosts read that fails exempts nothing.
"""
import asyncio
import socket

import httpx
import pytest

from app.utils import redirect_guard


@pytest.fixture
def lan(monkeypatch):
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host in ("media.example.org", "evil.example.org"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.1", 0))]
        return real(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    from app.services import search_service
    monkeypatch.setattr(search_service, "own_media_hosts", lambda: {"media.example.org"})
    return search_service


def _check(url):
    try:
        asyncio.run(redirect_guard._check(httpx.Request("GET", url)))
        return True
    except redirect_guard.RedirectRefused:
        return False


def test_our_own_media_host_passes_though_it_resolves_private(lan):
    assert _check("https://media.example.org/" + "a" * 64 + ".png"), "our own media was refused as private"


def test_any_other_name_for_a_private_address_is_still_refused(lan):
    assert not _check("https://evil.example.org/x.png")
    assert not _check("http://127.0.0.1:9997/v3/config/global/get")


def test_a_failed_own_hosts_read_exempts_nothing(lan, monkeypatch):
    def boom():
        raise RuntimeError("settings unavailable")
    monkeypatch.setattr(lan, "own_media_hosts", boom)
    assert not _check("https://media.example.org/x.png")


def test_the_image_proxy_serves_our_own_media(lan, monkeypatch):
    """End to end through the shipped `_proxy_fetch` (what /client/proxy-image calls), with the transport stubbed
    AFTER the guard: the request must reach the network layer and come back as image bytes."""
    from app.routers import chat
    seen = []

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append(str(request.url))
            return httpx.Response(200, content=b"\x89PNG....", headers={"content-type": "image/png"})
    real = httpx.AsyncClient
    # The stub REPLACES the shipped transport (_proxy_fetch now passes a PinnedTransport of its own).
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": T()}))
    content, ctype = asyncio.run(chat._proxy_fetch("https://media.example.org/" + "b" * 64 + ".png"))
    assert content.startswith(b"\x89PNG") and ctype == "image/png" and seen, (content, ctype, seen)
