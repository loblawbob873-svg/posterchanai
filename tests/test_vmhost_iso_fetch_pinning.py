"""`iso.fetch` must connect to the address it CHECKED, never to a second DNS answer, and never through a proxy.

The old guard resolved the name (`rss_service.is_safe_host`) and then let httpx resolve it AGAIN to connect: a DNS
server that answers a public address first and 127.0.0.1 (or 169.254.169.254) second — DNS rebinding — walked
straight past the check. It also let httpx read HTTP(S)_PROXY from the environment, and it did not block several
non-public ranges (CGNAT 100.64/10, 192.0.0.0/24, benchmarking 198.18/15, the NAT64 prefixes, IPv4-mapped forms).

These tests drive the SHIPPED production client: the real `httpx.AsyncHTTPTransport` is intercepted at
`handle_async_request`, which is the last point before a socket would open, and records which connection pool
(direct or proxy) and which host it was asked to reach. Nothing leaves this machine.
"""
import asyncio
import socket

import httpx
import pytest

from app.services.vmhost import isolib
from tests.test_vmhost_phase2 import ADMIN, make

PUBLIC = "93.184.216.34"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def wire(monkeypatch):
    """Record every request the real transport would put on the wire, and answer it with a tiny ISO."""
    seen = []

    async def handle(self, request):
        seen.append({"pool": type(self._pool).__name__, "url_host": request.url.host,
                     "host_header": request.headers.get("host"),
                     "sni": request.extensions.get("sni_hostname")})
        return httpx.Response(200, content=b"ISO-BYTES", request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return seen


def fake_dns(monkeypatch, answers):
    """getaddrinfo that answers `answers[host]` in turn (the last one repeats) — a rebinding DNS server."""
    real = socket.getaddrinfo
    calls = {}

    def gai(host, port, *a, **kw):
        if host in answers:
            i = calls.get(host, 0)
            calls[host] = i + 1
            ip = answers[host][min(i, len(answers[host]) - 1)]
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            return [(fam, socket.SOCK_STREAM, 6, "", (ip, port or 0) if fam == socket.AF_INET else (ip, port or 0, 0, 0))]
        return real(host, port, *a, **kw)
    monkeypatch.setattr(socket, "getaddrinfo", gai)
    return calls


def fetch(svc, url):
    return run(svc.handle(ADMIN, "iso.fetch", {"url": url}, "f"))


def test_a_rebinding_dns_answer_never_reaches_the_connection(tmp_path, monkeypatch, wire):
    svc, be, root = make(tmp_path)
    fake_dns(monkeypatch, {"rebind.example": [PUBLIC, "127.0.0.1", "169.254.169.254"]})
    res = fetch(svc, "https://rebind.example/debian.iso")
    assert all(w["url_host"] == PUBLIC for w in wire), f"connected by name (resolved again): {wire}"
    assert wire and wire[0]["host_header"] == "rebind.example" and wire[0]["sni"] == "rebind.example", wire
    assert res["ok"], res


def test_a_name_whose_answer_includes_a_private_address_is_refused_without_a_request(tmp_path, monkeypatch, wire):
    svc, be, root = make(tmp_path)
    fake_dns(monkeypatch, {"mixed.example": ["127.0.0.1"]})
    res = fetch(svc, "https://mixed.example/x.iso")
    assert not res["ok"] and res["error"]["code"] == "forbidden" and wire == [], (res, wire)


def test_an_environment_proxy_is_ignored(tmp_path, monkeypatch, wire):
    svc, be, root = make(tmp_path)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    fake_dns(monkeypatch, {"mirror.example": [PUBLIC]})
    res = fetch(svc, "http://mirror.example/alpine.iso")
    assert wire and all(w["pool"] == "AsyncConnectionPool" for w in wire), f"went through a proxy: {wire}"
    assert res["ok"], res


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.127.255.254", "192.0.0.9", "198.18.0.1", "198.19.255.1",
                                "64:ff9b::7f00:1", "64:ff9b:1::a9fe:a9fe", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
                                "::127.0.0.1", "224.0.0.1", "ff02::1", "0.0.0.0", "240.0.0.1", "169.254.169.254",
                                "fe80::1", "fc00::1", "::1", "2002:7f00:1::1"])
def test_every_non_public_range_is_blocked(tmp_path, monkeypatch, wire, ip):
    assert isolib.ip_blocked(ip), ip
    svc, be, root = make(tmp_path)
    fake_dns(monkeypatch, {"sneaky.example": [ip]})
    res = fetch(svc, "https://sneaky.example/x.iso")
    assert not res["ok"] and res["error"]["code"] == "forbidden" and wire == [], (ip, res, wire)


@pytest.mark.parametrize("ip", [PUBLIC, "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_not_blocked(ip):
    assert not isolib.ip_blocked(ip)


def test_a_redirect_is_resolved_and_pinned_again(tmp_path, monkeypatch):
    seen = []

    async def handle(self, request):
        seen.append((request.url.host, request.headers.get("host")))
        if request.headers.get("host") == "a.example":
            return httpx.Response(302, headers={"location": "https://b.example/real.iso"}, request=request)
        return httpx.Response(200, content=b"ISO", request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    svc, be, root = make(tmp_path)
    fake_dns(monkeypatch, {"a.example": [PUBLIC], "b.example": ["10.1.2.3"]})
    res = fetch(svc, "https://a.example/latest.iso")
    assert not res["ok"] and res["error"]["code"] == "forbidden", res
    assert seen == [(PUBLIC, "a.example")], seen
