"""A Meme Builder layer that is one of THIS instance's own files renders -- it is not "refused media source".

Reported 2026-10-27: "Meme builder render error: refused media source: https://poster.place/client/emoji/politics/
H1B_pajeet.png" -- a custom emoji of the instance itself. The render fetches its layers server-side behind an SSRF
guard; our own names resolve to a PRIVATE LAN address from inside the LAN (split-horizon DNS), so they are exempt --
but the exemption listed the Blossom hosts and the admin's extra hosts and never the instance's own `site_url`.
Driven through the shipped _fetch_media_guarded / _fetch_media_to_file with the network stubbed at httpx: the own
site fetches, a stranger that resolves privately is still refused.
"""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.routers import client as C
from app.services import search_service

SETTINGS = {"site_url": "https://poster.place", "blossom_public_url": "https://media.poster.place"}
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


@pytest.fixture
def net(monkeypatch):
    asked = []
    monkeypatch.setattr(search_service.settings_store, "get", lambda k, d=None: SETTINGS.get(k, d))
    monkeypatch.setattr("app.services.rss_service.is_safe_host", lambda u: False)      # everything looks private (LAN)
    monkeypatch.setattr("app.services.rss_service.looks_fetchable", lambda u: True)

    def handler(req):
        asked.append(str(req.url))
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler),
                                                                     **{x: y for x, y in k.items() if x != "transport"}))
    return asked


def test_the_instances_own_emoji_is_fetched_for_a_render(net, tmp_path):
    url = "https://poster.place/client/emoji/politics/H1B_pajeet.png"
    own = C._own_media_hosts(None)
    assert "poster.place" in own, own
    body, ct = asyncio.run(C._fetch_media_guarded(url, own))
    assert body == PNG and ct == "image/png"
    n = asyncio.run(C._fetch_media_to_file(url, own, str(tmp_path / "layer"), 10_000_000))
    assert n == len(PNG) and net == [url, url]


def test_a_stranger_that_resolves_privately_is_still_refused(net):
    own = C._own_media_hosts(None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(C._fetch_media_guarded("https://evil.example/x.png", own))
    assert e.value.status_code == 400 and net == []
