"""A LINK CARD SHOWS THE PAGE'S TITLE AS TEXT -- ENTITIES DECODED, APOSTROPHES KEPT.

Filed as the reporter's first bug (2026-10-30, "Formatted text in link previews"): a stablekraft.app album card read
"The Writing&#x27;s On The Wall". The page writes `content="The Writing&#x27;s On The Wall"` (measured), and
/client/preview handed that through undecoded. The same parser also cut a title at a plain `'` inside a double-quoted
attribute. Runs the shipped endpoint against a page shaped like the reported one.
"""
import asyncio
import json
from contextlib import asynccontextmanager

PAGE = b"""<html><head>
<meta property="og:title" content="The Writing&#x27;s On The Wall"/>
<meta property="og:description" content="Listen to The Writing&#x27;s On The Wall by Henrik Flyman &amp; friends"/>
<meta property="og:site_name" content="StableKraft"/>
<meta name="twitter:title" content="unused"/>
</head></html>"""
PLAIN = b"""<html><head><meta property="og:title" content="Don't Stop Me Now"><meta content='Say "hi"' property="og:description"></head></html>"""


def _serve(monkeypatch, body):
    from app.routers import client

    class Resp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}

        async def aiter_bytes(self):
            yield body

    class Fake:
        @asynccontextmanager
        async def stream(self, method, url):
            yield Resp()

    async def http():
        return Fake()

    async def public(host):
        return True
    monkeypatch.setattr(client, "_preview_http", http)
    monkeypatch.setattr(client, "_is_public_host_async", public)
    client._preview_cache.clear()
    return client


def test_entities_in_the_card_are_decoded(monkeypatch):
    client = _serve(monkeypatch, PAGE)
    d = json.loads(asyncio.run(client.link_preview("https://stablekraft.app/album/x")).body)
    assert d["title"] == "The Writing's On The Wall", d
    assert d["description"] == "Listen to The Writing's On The Wall by Henrik Flyman & friends", d
    assert d["site"] == "StableKraft"


def test_a_quote_of_the_other_kind_is_content_not_the_end(monkeypatch):
    client = _serve(monkeypatch, PLAIN)
    d = json.loads(asyncio.run(client.link_preview("https://example.com/song")).body)
    assert d["title"] == "Don't Stop Me Now" and d["description"] == 'Say "hi"', d


def test_old_cached_cards_are_not_reused():
    src = open("static/js/client/linkcards.js").read()
    assert "const K='pc-lpv-v2'" in src, "the client's preview cache must drop entries stored before the fix"
