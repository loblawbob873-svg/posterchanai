"""ISOs come in through BLOSSOM, not a per-host upload endpoint: the client uploads the ISO to the
media store it already uses, and the host PULLS the blob from its OWN Blossom by sha256
(`iso.fetch {blob}`). That needs no inbound route to the host — which is why the old per-host upload
ticket stalled for a host (nas) whose app is not publicly reachable.

Our Blossom (media.poster.place) resolves to a LAN address on purpose, so the SSRF guard that rightly
refuses every private address for an arbitrary URL would refuse it too. The carve-out is narrow: the
host builds the URL from ITS OWN configured `blossom_url`, and ONLY that exact host is allowed to
resolve privately, ONLY at hop 0 — a redirect off it, or any other private host, is still refused.

Drives the SHIPPED service/isolib: the real httpx transport is intercepted at the last point before a
socket opens, and DNS is a rebinding stub. Nothing leaves this machine.
"""
import asyncio
import socket

import httpx
import pytest

from app.services.vmhost import isolib
from tests.test_vmhost_phase2 import make, ADMIN
from tests.test_vmhost_iso_fetch_pinning import fake_dns, run

BLOSSOM = "https://media.example"          # this host's own Blossom (a LAN address in real life)
SHA = "a" * 64
PRIV = "10.1.2.3"


def wirefx(monkeypatch, redirect_to=None):
    seen = []

    async def handle(self, request):
        seen.append({"host": request.url.host, "host_header": request.headers.get("host")})
        if redirect_to and request.headers.get("host") == "media.example":
            return httpx.Response(302, headers={"location": redirect_to}, request=request)
        return httpx.Response(200, content=b"ISO-BYTES", request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return seen


async def fetch_blob_and_wait(svc, args):
    res = await svc.handle(ADMIN, "iso.fetch", args, "b", None)
    if not res.get("ok"):
        return res
    jid = res["result"]["job"]["id"]
    for _ in range(2000):
        job = svc._iso_jobs()[jid]
        if job["state"] != "running":
            break
        await asyncio.sleep(0.005)
    if job["state"] == "done":
        return {"ok": True, "result": {"iso": job["iso"]}}
    return {"ok": False, "error": {"code": job.get("code", "backend_error"), "message": job["error"]}}


def test_a_blob_is_pulled_from_our_own_blossom_even_on_a_lan_address(tmp_path, monkeypatch):
    # The address is one ip_blocked() rejects for any other host — proving the carve-out is doing the work.
    assert isolib.ip_blocked(PRIV)
    svc, be, root = make(tmp_path, blossom_url=BLOSSOM)
    seen = wirefx(monkeypatch)
    fake_dns(monkeypatch, {"media.example": [PRIV]})
    res = run(fetch_blob_and_wait(svc, {"blob": SHA, "name": "debian.iso"}))
    assert res["ok"], res
    assert res["result"]["iso"]["name"] == "debian.iso"
    assert seen and seen[0]["host"] == PRIV and seen[0]["host_header"] == "media.example", seen


def test_a_redirect_off_our_blossom_to_a_private_host_is_still_refused(tmp_path, monkeypatch):
    svc, be, root = make(tmp_path, blossom_url=BLOSSOM)
    seen = wirefx(monkeypatch, redirect_to="https://evil.example/x.iso")
    fake_dns(monkeypatch, {"media.example": [PRIV], "evil.example": ["169.254.169.254"]})
    res = run(fetch_blob_and_wait(svc, {"blob": SHA, "name": "x.iso"}))
    assert not res["ok"] and res["error"]["code"] == "forbidden", res
    # It reached our Blossom (hop 0, trusted) but never the redirect target.
    assert [s["host_header"] for s in seen] == ["media.example"], seen


def test_a_blob_with_no_blossom_configured_is_refused(tmp_path, monkeypatch):
    svc, be, root = make(tmp_path)                      # blossom_url defaults to ""
    wirefx(monkeypatch)
    res = run(fetch_blob_and_wait(svc, {"blob": SHA}))
    assert not res["ok"] and res["error"]["code"] == "forbidden", res


def test_a_non_sha_blob_is_rejected(tmp_path, monkeypatch):
    svc, be, root = make(tmp_path, blossom_url=BLOSSOM)
    wirefx(monkeypatch)
    res = run(fetch_blob_and_wait(svc, {"blob": "not-a-hash"}))
    assert not res["ok"] and res["error"]["code"] == "bad_request", res


def test_a_pasted_url_on_our_own_blossom_is_trusted_like_a_blob(tmp_path, monkeypatch):
    svc, be, root = make(tmp_path, blossom_url=BLOSSOM)
    seen = wirefx(monkeypatch)
    fake_dns(monkeypatch, {"media.example": [PRIV]})
    res = run(fetch_blob_and_wait(svc, {"url": BLOSSOM + "/" + SHA, "name": "u.iso"}))
    assert res["ok"], res
    assert seen and seen[0]["host_header"] == "media.example", seen


def test_a_foreign_private_url_is_still_blocked(tmp_path, monkeypatch):
    # No blob, a plain URL to a NON-Blossom host that resolves privately: the guard must still refuse it,
    # with no request on the wire — the carve-out must not have widened the SSRF surface.
    svc, be, root = make(tmp_path, blossom_url=BLOSSOM)
    seen = wirefx(monkeypatch)
    fake_dns(monkeypatch, {"intranet.example": ["127.0.0.1"]})
    res = run(fetch_blob_and_wait(svc, {"url": "https://intranet.example/secret.iso"}))
    assert not res["ok"] and res["error"]["code"] == "forbidden" and seen == [], (res, seen)
