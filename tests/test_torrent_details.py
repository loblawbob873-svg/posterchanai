"""Torrents → Nyaa/TGX → ℹ Details: a torrent's description and file list, read from its site's page.

Asked for: "Add feature to show torrent details from the site in Torrents so you can read the
description." The parsers run against pages captured from the real sites (trimmed), the URL is built
from an id and nothing else, and the TGX link our search hands out actually opens (it was a 404).
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.services import torrent_details as td

FIX = Path(__file__).parent / "fixtures" / "torrent_details"


def test_a_nyaa_page_gives_its_description_details_and_files():
    d = td.parse_nyaa((FIX / "nyaa_view.html").read_text())
    assert "Sousou no Frieren" in d["title"]
    info = dict(d["info"])
    assert info["Submitter"] == "nekomoekissaten" and info["File size"] == "7.6 GiB", info
    assert info["Info hash"] == "8e4e8471b226e311eb7b3b77476eec7834052dbd"
    assert "Nekomoekissaten-Subs GitHub repo" in d["description"]
    assert "https://github.com/Nekomoekissaten-SUB/Nekomoekissaten-Subs" in d["description"]
    assert d["file_count"] == 10 and d["files"][0] == ["[Nekomoe kissaten][Sousou no Frieren][29][1080p][JPTC].mp4", "793.3 MiB"]


def test_a_tgx_page_gives_its_description_details_and_files():
    d = td.parse_tgx((FIX / "tgx_view.html").read_text())
    assert d["title"].startswith("Frieren Beyond Journeys End S01"), d["title"]
    info = dict(d["info"])
    assert info["Total Size"] == "6.3 GB" and info["Added By"] == "RaptaGzus", info
    assert info["Info Hash"] == "FD41EFFC804FB49AE2E25150E0E1ED8B393D6D10"
    assert "Stars: Atsumi Tanezaki" in d["description"] and "Included subtitles" in d["description"]
    assert "<img" not in d["description"] and "<br" not in d["description"]
    assert d["file_count"] == 28 and d["files"][0] == ["Frieren - Beyond Journey's End - S01E01.mkv", "270.1 MB"]


@pytest.mark.parametrize("src,ident", [
    ("nyaa", "../etc"), ("nyaa", "1/../../x"), ("nyaa", "http://169.254.169.254/"), ("tgx", "abc/../x"),
    ("tgx", "zz"), ("other", "1"), ("nyaa", ""),
])
def test_the_page_address_is_built_from_an_id_never_taken(src, ident):
    with pytest.raises(ValueError):
        td.page_url(src, ident, "https://torrentgalaxy.one")


def test_page_urls():
    assert td.page_url("nyaa", "2175192", "x") == "https://nyaa.si/view/2175192"
    assert td.page_url("tgx", "8F1596", "https://torrentgalaxy.one/") == "https://torrentgalaxy.one/post-detail/8f1596/torrent/"


def test_a_tgx_search_result_links_to_a_page_that_exists(monkeypatch):
    """`/post-detail/<pk>/` alone is a 404 on TGX; its own pages always carry a slug after the key."""
    from app.services import torrent_service as ts
    payload = {"results": [{"pk": "8f1596", "n": "Frieren Beyond Journeys End S01 1080p", "h": "FD41EFFC804FB49AE2E25150E0E1ED8B393D6D10",
                            "s": 6753086437, "se": 304, "le": 152, "c": "TV"}], "total": 1}

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return Resp()
    monkeypatch.setattr(ts.httpx, "AsyncClient", Client)
    monkeypatch.setattr("app.services.proxy_utils.require_proxy", lambda name: "http://proxy.invalid:1")
    monkeypatch.setattr(ts, "get_torrent_base_url", lambda db: "https://torrentgalaxy.one")
    rows = asyncio.run(ts.search_torrents(None, "frieren", 5))
    assert rows[0].url == "https://torrentgalaxy.one/post-detail/8f1596/frieren-beyond-journeys-end-s01-1080p/", rows[0].url


def test_details_are_fetched_once_through_the_proxy_and_cached(monkeypatch):
    td._cache.clear()
    calls = []

    class Resp:
        status_code = 200
        text = (FIX / "nyaa_view.html").read_text()
        def raise_for_status(self): pass

    class Client:
        def __init__(self, *a, **k): calls.append(k.get("proxy"))
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k):
            calls.append(url)
            return Resp()
    monkeypatch.setattr(td.httpx, "AsyncClient", Client)
    a = asyncio.run(td.fetch_details("nyaa", "2175192", "", "http://proxy.invalid:1"))
    b = asyncio.run(td.fetch_details("nyaa", "2175192", "", "http://proxy.invalid:1"))
    assert a == b and a["url"] == "https://nyaa.si/view/2175192"
    assert calls == ["http://proxy.invalid:1", "https://nyaa.si/view/2175192"], ("one request, through the proxy", calls)


def test_the_route_refuses_what_it_cannot_build(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import torrent as R
    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[R.get_torrent_user] = lambda: object()
    app.dependency_overrides[R.get_db] = lambda: None
    monkeypatch.setattr("app.services.proxy_utils.require_proxy", lambda name: "http://proxy.invalid:1")
    c = TestClient(app)
    assert c.get("/api/torrent/details?src=evil&id=1").status_code == 400
    assert c.get("/api/torrent/details?src=nyaa&id=../x").status_code == 400

    async def gone(*a, **k):
        raise LookupError("That torrent is no longer on the site.")
    monkeypatch.setattr(td, "fetch_details", gone)
    r = c.get("/api/torrent/details?src=nyaa&id=1")
    assert r.status_code == 404 and json.loads(r.text)["detail"].startswith("That torrent")
