"""The endpoints behind Torrents → Nyaa and TGX.

  * `/api/torrent/nyaa` with NO query lists nyaa.si's newest uploads (its front page), which is what
    the Nyaa tab opens on; with a query it searches. The `nyaa` chat command still answers an empty
    query with its usage text, so "no query = the front page" is opt-in (`latest=True`) and only the
    tab asks for it.
  * `/nyaa`, `/catalog` and `/search` answer in one row shape, which is what the tabs draw.

The nyaa parser runs for real against a page in nyaa's own markup; only the network is a fixture.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import torrent as routes
from app.services import nyaa_service

PAGE = """<html><body><table class="torrent-list"><thead><tr><th>c</th></tr></thead><tbody>
<tr><td>Anime</td><td><a href="/view/1">[Sub] Newest Show - 01 [1080p]</a></td>
<td><a href="/download/1.torrent">t</a><a href="magnet:?xt=urn:btih:1111111111111111111111111111111111111111">m</a></td>
<td>1.2 GiB</td><td>2026-09-23</td><td>150</td><td>9</td><td>300</td></tr>
<tr><td>Anime</td><td><a href="/view/2">[Sub] Older Show - 12 [720p]</a></td>
<td><a href="magnet:?xt=urn:btih:2222222222222222222222222222222222222222">m</a></td>
<td>700.0 MiB</td><td>2026-09-22</td><td>80</td><td>3</td><td>120</td></tr>
</tbody></table></body></html>"""


@pytest.fixture
def fake_nyaa(monkeypatch):
    asked = []

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            asked.append(url)
            return SimpleNamespace(text=PAGE, raise_for_status=lambda: None)

    monkeypatch.setattr(nyaa_service.httpx, "AsyncClient", Client)
    import app.services.proxy_utils as proxy_utils
    monkeypatch.setattr(proxy_utils, "require_proxy", lambda what: "http://127.0.0.1:8118")
    return asked


def test_an_empty_query_is_the_newest_uploads_only_when_asked_for(fake_nyaa):
    assert asyncio.run(nyaa_service.search_nyaa("", limit=5)) == []
    assert fake_nyaa == [], "an empty query reached the network without latest=True"

    rows = asyncio.run(nyaa_service.search_nyaa("  ", limit=5, latest=True))
    assert [r.title for r in rows] == ["[Sub] Newest Show - 01 [1080p]", "[Sub] Older Show - 12 [720p]"]
    assert rows[0].seeders == 150 and rows[0].leechers == 9 and rows[0].size == "1.2 GiB"
    assert fake_nyaa[-1] == "https://nyaa.si/?f=0&c=0_0", fake_nyaa

    asyncio.run(nyaa_service.search_nyaa("one piece", limit=5, latest=True))
    assert fake_nyaa[-1].endswith("&q=one+piece"), fake_nyaa


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_torrent_user] = lambda: None
    app.dependency_overrides[routes.get_db] = lambda: None
    with TestClient(app) as client:
        yield client


def test_the_nyaa_tab_endpoint_lists_newest_without_a_query(api, fake_nyaa):
    body = api.get("/api/torrent/nyaa", params={"limit": 75}).json()
    assert [i["num"] for i in body["items"]] == [1, 2]
    assert set(body["items"][0]) == {"num", "title", "magnet", "size", "seeders", "leechers", "url"}
    assert body["items"][0]["magnet"].startswith("magnet:?xt=urn:btih:1111")
    assert fake_nyaa[-1] == "https://nyaa.si/?f=0&c=0_0"
    assert api.get("/api/torrent/nyaa", params={"q": "frieren"}).status_code == 200
    assert fake_nyaa[-1].endswith("&q=frieren")
    assert api.get("/api/torrent/nyaa", params={"limit": 76}).status_code == 422


def test_catalog_and_search_answer_in_the_same_row_shape(api, monkeypatch):
    row = SimpleNamespace(title="Film", magnet="magnet:?xt=urn:btih:" + "3" * 40, size="2 GB",
                          seeders=5, leechers=1, url=None)
    async def scrape(db, category, limit): return [row] if category == "tv" else []
    async def search(db, q, limit): return [row, row]
    monkeypatch.setattr(routes, "scrape_torrents", scrape)
    monkeypatch.setattr(routes, "search_torrents", search)
    cat = api.get("/api/torrent/catalog", params={"category": "tv"}).json()
    assert cat["category"] == "tv" and cat["items"][0] == {
        "num": 1, "title": "Film", "magnet": row.magnet, "size": "2 GB", "seeders": 5, "leechers": 1, "url": ""}
    assert [i["num"] for i in api.get("/api/torrent/search", params={"q": "x"}).json()["items"]] == [1, 2]
    assert api.get("/api/torrent/catalog", params={"category": "games"}).status_code == 400
