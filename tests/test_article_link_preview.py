"""A SHARED ARTICLE LINK (`poster.place/<naddr1…>` of a kind 30023) HAS A REAL LINK PREVIEW.

"our article links https://poster.place/naddr1qvzqqqr4gupzqj6kh06peyh9sm5gjfavk7yrd66f72ccgzq7lpfxyh8h30na267kqq9kgunpve6z6dncxp3nvj9she7
are not showing as link previews when shared" (2026-10-30): the /{entity} route served the client shell with no
OpenGraph, so every article previewed as the bare app title. Runs the shipped route, the shipped naddr decoder against
the reported link, and the shipped template for what lands in <head>.
"""
import asyncio
import re

import pytest
from starlette.requests import Request

REPORTED = "naddr1qvzqqqr4gupzqj6kh06peyh9sm5gjfavk7yrd66f72ccgzq7lpfxyh8h30na267kqq9kgunpve6z6dncxp3nvj9she7"
HEX = "4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6"


def _request(path):
    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
             "scheme": "http", "server": ("poster.place", 80), "client": ("1.2.3.4", 1), "root_path": "",
             "headers": [(b"host", b"poster.place"), (b"x-forwarded-proto", b"https")]}
    return Request(scope)


def test_the_reported_link_decodes_to_an_article_and_a_typo_does_not():
    from app.services import article_share
    ref = article_share.decode_naddr(REPORTED)
    assert ref and ref["kind"] == 30023 and ref["pubkey"] == HEX and ref["d"] == "draft-6x0c6", ref
    assert article_share.decode_naddr(REPORTED[:-1] + "8") is None, "a bad checksum must not decode"
    assert article_share.decode_naddr("npub1" + REPORTED[6:]) is None


@pytest.fixture
def page(monkeypatch):
    import app.main as m
    from app.routers import client
    from app.services import article_share
    seen = {}

    async def shell(request, db, meta=None):
        seen["meta"] = meta
        return "SHELL"

    async def plain(request, *a, **k):
        seen["plain"] = True
        return "PLAIN"
    monkeypatch.setattr(client, "render_client_shell", shell)
    monkeypatch.setattr(client, "client_app", plain)
    return m, seen, article_share


def test_an_article_gets_its_title_summary_image_and_author(page, monkeypatch):
    m, seen, article_share = page

    async def card(port, ref):
        assert ref["d"] == "draft-6x0c6"
        return {"title": 'Why "PosterChanOS"', "summary": "Private, and still yours.", "image": "https://media.poster.place/c.png",
                "author": "Verita84", "published": 1}
    monkeypatch.setattr(article_share, "article_card", card)
    assert asyncio.run(m.nostr_entity_page(REPORTED, _request("/" + REPORTED))) == "SHELL"
    meta = seen["meta"]
    assert meta["title"] == 'Why "PosterChanOS" · Verita84' and meta["type"] == "article", meta
    assert meta["url"] == "https://poster.place/" + REPORTED, "og:url must be the address the reader is on"
    assert meta["image"] == "https://media.poster.place/c.png" and meta["description"] == "Private, and still yours."


def test_an_article_this_node_cannot_read_and_other_entities_are_served_as_before(page, monkeypatch):
    m, seen, article_share = page

    async def none(port, ref):
        return None
    monkeypatch.setattr(article_share, "article_card", none)
    assert asyncio.run(m.nostr_entity_page(REPORTED, _request("/" + REPORTED))) == "PLAIN"
    assert "meta" not in seen
    npub = "npub1fdtthaq7f3d4yv8ufxs2t5ppsr36tt6x7t4ww5c2mp4npfxpvc4qffyu4z"
    assert asyncio.run(m.nostr_entity_page(npub, _request("/" + npub))) == "PLAIN"


def test_the_preview_lands_in_the_shipped_head_escaped(monkeypatch):
    from app.routers import client as client_router
    from app.services import article_share
    monkeypatch.setattr(client_router, "_default_theme", lambda db: "cyberpunk")
    meta = article_share.og_meta({"title": 'Quote " breaks <out>', "summary": "a & b", "image": "", "author": ""},
                                 "https://poster.place/" + REPORTED, fallback_image="https://poster.place/static/icon-512.png")
    resp = asyncio.run(client_router.render_client_shell(_request("/" + REPORTED), db=None, meta=meta))
    html = resp.body.decode() if hasattr(resp, "body") else str(resp)
    head = html[:html.index("</head>")]
    m = re.search(r'<meta property="og:title" content="([^"]*)"', head)
    assert m, "og:title is not in <head>"
    assert "<out>" not in head and 'breaks <' not in head, "the title was not escaped"
    assert 'property="og:type" content="article"' in head
