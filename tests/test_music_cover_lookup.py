"""/api/music/cover -- the Music library's opt-in online album covers, against a FAKE MusicBrainz + Cover Art Archive.

No network: the service's outbound transport is replaced by an httpx.MockTransport that plays both services and
counts every call. Each rule in app/services/music_cover_service.py is a case here:
  happy path; a cache hit asks nobody; "no cover" is cached and expires; "could not ask" is NOT cached and is a 503;
  an oversize, non-image, undecodable answer or a redirect off the archive is refused; <= 1 request a second to
  MusicBrainz under concurrency; a per-user rate limit; members only; the artist/album never reach the log.
"""
import asyncio
import io
import logging
import os
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import music_cover as router_mod
from app.services import music_cover_service as svc

RG = "1d1d1d1d-2e2e-3f3f-4a4a-5b5b5b5b5b5b"
RG2 = "2d1d1d1d-2e2e-3f3f-4a4a-5b5b5b5b5b5b"
ARTIST, ALBUM = "Zyxw Secret Artist", "Qwerty Private Album"


def _png(size=8):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (size, size), (200, 30, 120)).save(b, "PNG")
    return b.getvalue()


class FakeWeb:
    """MusicBrainz + the Cover Art Archive, as httpx sees them."""

    def __init__(self):
        self.calls, self.mb_times = [], []
        self.mb = lambda req: httpx.Response(200, json={"release-groups": [{"id": RG, "score": 100}]})
        self.caa = lambda req: httpx.Response(307, headers={"location": "https://ia800.us.archive.org/x/front-500.png"})
        self.ia = lambda req: httpx.Response(200, headers={"content-type": "image/png"}, content=_png())

    def handler(self, req: httpx.Request):
        self.calls.append(str(req.url))
        host = req.url.host
        if host == "musicbrainz.org":
            self.mb_times.append(time.monotonic())
            return self.mb(req)
        if host == "coverartarchive.org":
            return self.caa(req)
        if host.endswith("archive.org"):
            return self.ia(req)
        return httpx.Response(599)


@pytest.fixture
def web(tmp_path, monkeypatch):
    fake = FakeWeb()
    monkeypatch.setenv("PC_MUSIC_COVER_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(svc, "_transport", lambda: httpx.MockTransport(fake.handler))
    monkeypatch.setattr(svc, "MB_MIN_INTERVAL", 0.0)          # the rate test sets it back to 1 s itself
    monkeypatch.setattr(svc, "_mb_last", 0.0)
    router_mod._recent.clear()
    return fake


class _U:
    def __init__(self, i):
        self.id = i


@pytest.fixture
def client(web):
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[router_mod.get_music_member] = lambda: _U(1)
    return TestClient(app)


def _ask(client, artist=ARTIST, album=ALBUM):
    return client.post("/api/music/cover", json={"artist": artist, "album": album})


def test_an_album_gets_its_cover_and_the_bytes_are_served_by_id(client, web):
    r = _ask(client)
    assert r.status_code == 200 and r.json() == {"id": RG}, r.text
    img = client.get(f"/api/music/cover/{RG}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == _png()
    assert img.headers.get("x-content-type-options") == "nosniff"
    mb = [c for c in web.calls if "musicbrainz" in c]
    assert len(mb) == 1 and "/release-group/" in mb[0] and "fmt=json" in mb[0], web.calls


def test_a_second_ask_is_answered_from_the_cache_and_asks_nobody(client, web):
    assert _ask(client).status_code == 200
    n = len(web.calls)
    for _ in range(3):
        assert _ask(client).json() == {"id": RG}
        assert client.get(f"/api/music/cover/{RG}").status_code == 200
    assert len(web.calls) == n, ("a cached cover asked the internet again", web.calls[n:])
    # ...the same album typed differently is the same question
    assert _ask(client, ARTIST.upper() + "  ", "  " + ALBUM.lower()).json() == {"id": RG}
    assert len(web.calls) == n


def test_no_cover_is_cached_for_a_week_and_then_asked_again(client, web, tmp_path):
    web.mb = lambda req: httpx.Response(200, json={"release-groups": []})
    assert _ask(client).status_code == 404
    n = len(web.calls)
    assert _ask(client).status_code == 404 and len(web.calls) == n, "a cached 'none' asked MusicBrainz again"
    # A week later the answer is old: ask again (and this time there is one).
    for p in (tmp_path / "covers" / "map").iterdir():
        old = time.time() - svc.NEGATIVE_TTL - 60
        os.utime(p, (old, old))
    web.mb = lambda req: httpx.Response(200, json={"release-groups": [{"id": RG, "score": 99}]})
    assert _ask(client).json() == {"id": RG}
    assert len(web.calls) > n


def test_a_low_score_match_is_not_believed(client, web):
    web.mb = lambda req: httpx.Response(200, json={"release-groups": [{"id": RG, "score": 40}]})
    assert _ask(client).status_code == 404


@pytest.mark.parametrize("failure", ["connect", "mb503", "mb429", "caa502", "timeout"])
def test_could_not_ask_is_a_503_and_is_never_cached(client, web, monkeypatch, failure):
    def boom(req):
        raise httpx.ConnectError("tor is down", request=req)
    if failure == "connect":
        web.mb = boom
    elif failure == "mb503":
        web.mb = lambda req: httpx.Response(503)
    elif failure == "mb429":
        web.mb = lambda req: httpx.Response(429)
    elif failure == "caa502":
        web.caa = lambda req: httpx.Response(502)
    else:
        monkeypatch.setattr(svc, "DEADLINE", 0.3)

        async def slow(req):
            await asyncio.sleep(2)
            return httpx.Response(200, json={})
        web.mb = None
        monkeypatch.setattr(svc, "_transport", lambda: httpx.MockTransport(slow))
    r = _ask(client)
    assert r.status_code == 503, (failure, r.status_code, r.text)
    assert "retry-after" in {k.lower() for k in r.headers}
    # Nothing was written as an answer: the next ask, with the service back, gets the cover.
    web.mb = lambda req: httpx.Response(200, json={"release-groups": [{"id": RG, "score": 100}]})
    web.caa = lambda req: httpx.Response(307, headers={"location": "https://ia800.us.archive.org/x/front-500.png"})
    monkeypatch.setattr(svc, "_transport", lambda: httpx.MockTransport(web.handler))
    assert _ask(client).json() == {"id": RG}, (failure, "an outage was remembered as 'no cover'")


@pytest.mark.parametrize("bad", ["oversize", "oversize_streamed", "not_image", "undecodable", "off_archive", "svg"])
def test_an_unacceptable_answer_is_refused(client, web, bad):
    if bad == "oversize":
        web.ia = lambda req: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8" + b"x" * (svc.MAX_BYTES + 10))
    elif bad == "oversize_streamed":
        # No Content-Length (chunked): only counting the bytes as they arrive can refuse it.
        async def chunks():
            yield _png()
            for _ in range(40):
                yield b"x" * 65536
        web.ia = lambda req: httpx.Response(200, headers={"content-type": "image/png"}, content=chunks())
    elif bad == "not_image":
        web.ia = lambda req: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<script>alert(1)</script>")
    elif bad == "undecodable":
        web.ia = lambda req: httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG not really")
    elif bad == "off_archive":
        web.caa = lambda req: httpx.Response(302, headers={"location": "https://evil.example/cover.png"})
    else:
        web.ia = lambda req: httpx.Response(200, headers={"content-type": "image/svg+xml"},
                                            content=b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    assert _ask(client).status_code == 404, bad
    assert client.get(f"/api/music/cover/{RG}").status_code == 404, ("an unacceptable cover was stored", bad)
    assert not any("evil.example" in c for c in web.calls), "a redirect off the archive was followed"


def test_musicbrainz_is_asked_at_most_once_a_second_under_concurrency(web, monkeypatch):
    monkeypatch.setattr(svc, "MB_MIN_INTERVAL", 1.0)
    albums = ["One", "Two", "Three"]

    async def go():
        return await asyncio.gather(*(svc.lookup("Artist", a) for a in albums))
    out = asyncio.run(go())
    assert [x.kind for x in out] == ["ok"] * 3
    t = sorted(web.mb_times)
    assert len(t) == 3, web.mb_times
    gaps = [b - a for a, b in zip(t, t[1:])]
    assert all(g >= 0.95 for g in gaps), ("MusicBrainz was asked faster than once a second", gaps)


def test_a_person_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(router_mod, "RATE_N", 3)
    codes = [_ask(client, album=f"Album {i}").status_code for i in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429], codes


def test_members_only(web):
    app = FastAPI()
    app.include_router(router_mod.router)
    from app.database import get_db
    app.dependency_overrides[get_db] = lambda: iter([None])
    c = TestClient(app)
    assert c.post("/api/music/cover", json={"artist": "a", "album": "b"}).status_code in (401, 403)
    assert c.get(f"/api/music/cover/{RG}").status_code in (401, 403)
    assert web.calls == [], "an anonymous request reached MusicBrainz"


def test_a_bad_id_is_never_fetched(client, web):
    assert client.get("/api/music/cover/..%2F..%2Fetc").status_code == 404
    assert client.get("/api/music/cover/not-a-uuid").status_code == 404
    assert web.calls == []


def test_the_log_never_names_the_artist_or_the_album(client, web, caplog):
    caplog.set_level(logging.DEBUG)
    assert _ask(client).status_code == 200
    web.mb = lambda req: httpx.Response(503)
    _ask(client, "Other Hidden Band", "Another Hidden Record")
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (ARTIST, ALBUM, "Other Hidden Band", "Another Hidden Record", "Zyxw", "Qwerty"):
        assert secret.lower() not in text.lower(), ("the log names what somebody listens to", secret, text)
    assert "music cover lookup" in text      # ...while still saying what happened



def test_the_names_never_travel_in_a_url(client, web):
    """The access log (uvicorn's, and the reverse proxy's) writes every request URL: the lookup is a POST with a
    body, and a GET carrying the names is refused rather than served."""
    r = client.get("/api/music/cover", params={"artist": ARTIST, "album": ALBUM})
    assert r.status_code == 405, r.status_code
    assert web.calls == []
