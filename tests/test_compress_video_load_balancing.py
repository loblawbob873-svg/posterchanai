"""/client/media/compress-video spreads across the fleet when this node is full — and can never lose a post.

Run: venv-unified/bin/python -m pytest tests/test_compress_video_load_balancing.py

The composer sends every posted clip over 2 MB here before uploading it. This node encodes two at a
time; a third used to be refused with a 429 by the anonymous-helper middleware (whose SHARED cap of 2
was the same number), and the client — rightly — then uploads the original. So a burst of video posts
went up uncompressed while nas's encoder sat idle.

Now: overflow only. When both local encoder slots are taken (or this node has no ffmpeg) the clip is
handed to a peer from `chat_server_urls`, round-robin, with a loop-guard header, and the peer's smaller
MP4 comes back. Every way a peer can fail — busy (429), broken (5xx), down, a non-video answer, a
result no smaller than the original — falls through to the local queue, which is exactly the
behaviour before this existed. The middleware gives this path its own pool (4) so the balancer can
be reached at all.

Each policy branch is driven through the REAL endpoint with stub peers; one test runs a real second
"node" (the same router) with real ffmpeg end to end.
"""
import asyncio
import os
import shutil
import subprocess

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from app.routers import client as C
from app.services import media_service as M, video_factory

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="the local path runs ffmpeg")

PEERS = ["http://peer-a:3051", "http://peer-b:3051"]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("VIDEO_ENCODER", "libx264")
    monkeypatch.setattr(M, "_video_encoder_cache", None)
    monkeypatch.setattr(C, "_compress_busy", [0])
    monkeypatch.setattr(C, "_compress_rr", [0])
    monkeypatch.setattr(video_factory, "parse_video_server_urls", lambda raw: list(PEERS))


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """A detailed clip the 'phone' encoded at a high rate — the local ceiling encode shrinks it."""
    p = str(tmp_path_factory.mktemp("lb") / "phone.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=30,noise=alls=10:allf=t", "-t", "3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "6M", "-pix_fmt", "yuv420p", p],
                   check=True, timeout=120)
    with open(p, "rb") as fh:
        return fh.read()


def _origin():
    app = FastAPI()
    app.include_router(C.router)
    return app


class StubPeers:
    """Records what each peer received; answers per host from a script."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []
        self.app = FastAPI()

        @self.app.post("/client/media/compress-video")
        async def endpoint(request: Request):
            host = request.headers.get("host", "").split(":")[0]
            body = await request.body()
            self.calls.append({"host": host, "fwd": request.headers.get("x-pcai-compress-fwd"),
                               "size": len(body)})
            answer = self.answers[host]
            if isinstance(answer, Exception):
                raise answer
            return answer

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(self.app, raise_app_exceptions=True),
                                 timeout=30)


def _post(clip, headers=None):
    with TestClient(_origin()) as tc:
        return tc.post("/client/media/compress-video", headers=headers or {},
                       files={"file": ("phone.mp4", clip, "video/mp4")})


def _use(monkeypatch, stubs):
    monkeypatch.setattr(C, "_compress_peer_client", stubs.client)


def _small_mp4(clip):
    return Response(content=b"\x00\x00\x00\x18ftypmp42" + b"x" * 1000, media_type="video/mp4")


# ---- when it stays here ------------------------------------------------------------------------

def test_a_free_node_encodes_locally_and_never_asks_a_peer(monkeypatch, clip):
    stubs = StubPeers({"peer-a": _small_mp4(clip), "peer-b": _small_mp4(clip)})
    _use(monkeypatch, stubs)
    r = _post(clip)
    assert r.status_code == 200 and len(r.content) < len(clip)
    assert stubs.calls == [], "a node with a free encoder slot must not ship the clip across the LAN"
    assert C._compress_busy[0] == 0, "the busy count must return to zero after a local encode"


def test_a_forwarded_job_is_never_forwarded_again(monkeypatch, clip):
    """The loop guard: a peer that is ALSO full encodes what it was handed rather than bouncing it."""
    stubs = StubPeers({"peer-a": _small_mp4(clip), "peer-b": _small_mp4(clip)})
    _use(monkeypatch, stubs)
    C._compress_busy[0] = C._COMPRESS_VIDEO_SLOTS
    r = _post(clip, headers={"x-pcai-compress-fwd": "1"})
    assert r.status_code == 200
    assert stubs.calls == []


def test_no_peers_configured_is_the_old_behaviour(monkeypatch, clip):
    monkeypatch.setattr(video_factory, "parse_video_server_urls", lambda raw: [])
    C._compress_busy[0] = C._COMPRESS_VIDEO_SLOTS
    r = _post(clip)
    assert r.status_code == 200 and len(r.content) < len(clip)


# ---- when it goes to a peer --------------------------------------------------------------------

def test_a_full_node_hands_the_clip_to_a_peer(monkeypatch, clip):
    stubs = StubPeers({"peer-a": _small_mp4(clip), "peer-b": _small_mp4(clip)})
    _use(monkeypatch, stubs)
    C._compress_busy[0] = C._COMPRESS_VIDEO_SLOTS
    r = _post(clip)
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert r.content.startswith(b"\x00\x00\x00\x18ftyp"), "the browser must get the PEER's result"
    assert len(stubs.calls) == 1
    call = stubs.calls[0]
    assert call["fwd"] == "1", "a forward must carry the loop-guard header"
    assert call["size"] > len(clip), "the peer must receive the whole clip (multipart adds framing)"


def test_forwards_rotate_across_peers(monkeypatch, clip):
    stubs = StubPeers({"peer-a": _small_mp4(clip), "peer-b": _small_mp4(clip)})
    _use(monkeypatch, stubs)
    C._compress_busy[0] = C._COMPRESS_VIDEO_SLOTS
    for _ in range(4):
        assert _post(clip).status_code == 200
    assert [c["host"] for c in stubs.calls] == ["peer-a", "peer-b", "peer-a", "peer-b"]


def test_a_node_without_ffmpeg_forwards_and_without_peers_says_so(monkeypatch, clip):
    monkeypatch.setattr(M, "ffmpeg_available", lambda: False)
    stubs = StubPeers({"peer-a": _small_mp4(clip), "peer-b": _small_mp4(clip)})
    _use(monkeypatch, stubs)
    assert _post(clip).status_code == 200 and len(stubs.calls) == 1
    monkeypatch.setattr(video_factory, "parse_video_server_urls", lambda raw: [])
    assert _post(clip).status_code == 503, "no ffmpeg and nobody to ask is still the honest 503"


def test_a_peer_that_cannot_do_better_is_passed_through_as_keep_the_original(monkeypatch, clip):
    stubs = StubPeers({"peer-a": Response(status_code=204), "peer-b": Response(status_code=204)})
    _use(monkeypatch, stubs)
    C._compress_busy[0] = C._COMPRESS_VIDEO_SLOTS
    assert _post(clip).status_code == 204
    assert len(stubs.calls) == 1


def test_a_peer_result_no_smaller_than_the_original_is_not_used(monkeypatch, clip):
    big = Response(content=b"\x00" * (len(clip) + 10), media_type="video/mp4")
    stubs = StubPeers({"peer-a": big, "peer-b": big})
    _use(monkeypatch, stubs)
    C._compress_busy[0] = C._COMPRESS_VIDEO_SLOTS
    assert _post(clip).status_code == 204


# ---- every failure falls back to the local queue ------------------------------------------------

@pytest.mark.parametrize("a,b", [
    (Response(status_code=429), Response(status_code=503)),
    (Response(status_code=500), httpx.ConnectError("peer-b is down")),
    (Response(content=b'{"error":"x"}', media_type="application/json"), Response(status_code=502)),
])
def test_when_every_peer_fails_the_clip_is_encoded_here(monkeypatch, clip, a, b):
    stubs = StubPeers({"peer-a": a, "peer-b": b})
    _use(monkeypatch, stubs)
    C._compress_busy[0] = C._COMPRESS_VIDEO_SLOTS
    r = _post(clip)
    assert r.status_code == 200 and len(r.content) < len(clip), \
        "a refused forward must never cost the post its compression"
    assert sorted(c["host"] for c in stubs.calls) == ["peer-a", "peer-b"], "every peer gets a turn first"


# ---- the real thing: a second node running the same router --------------------------------------

def test_end_to_end_a_real_peer_compresses_the_overflow(monkeypatch, clip):
    peer = _origin()
    monkeypatch.setattr(video_factory, "parse_video_server_urls", lambda raw: ["http://peer-a:3051"])
    monkeypatch.setattr(C, "_compress_peer_client",
                        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(peer), timeout=120))
    seen = []
    real = C._compress_lb_forward

    async def spy(request, *a, **k):
        seen.append(bool(request.headers.get("x-pcai-compress-fwd")))
        return await real(request, *a, **k)
    monkeypatch.setattr(C, "_compress_lb_forward", spy)
    C._compress_busy[0] = C._COMPRESS_VIDEO_SLOTS
    r = _post(clip)
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert len(r.content) < len(clip)
    assert seen == [False, True], "the origin forwarded, and the peer ran it itself (loop guard held)"


# ---- the middleware lets the balancer be reached --------------------------------------------------

def test_the_middleware_gives_video_its_own_pool():
    """A shared cap of 2 refused the 3rd video with a 429 before the balancer could see it — and two
    videos in flight locked translate/STT/narrate out of the node."""
    from app.middleware.public_work import PublicWorkMiddleware

    async def run():
        release = asyncio.Event()
        started = asyncio.Semaphore(0)
        app = FastAPI()

        @app.post("/client/media/compress-video")
        async def video():
            started.release()
            await release.wait()
            return {"ok": True}

        @app.post("/client/translate")
        async def translate():
            return {"ok": True}

        mw = PublicWorkMiddleware(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(mw), base_url="http://t") as c:
            pool = PublicWorkMiddleware.POOLS["/client/media/compress-video"]
            held = [asyncio.create_task(c.post("/client/media/compress-video")) for _ in range(pool)]
            try:
                for _ in range(pool):
                    await asyncio.wait_for(started.acquire(), 5)
                assert pool > PublicWorkMiddleware.SHARED_CAP, "the pool must exceed the encoder slots"
                assert (await c.post("/client/translate")).status_code == 200, \
                    "videos in flight must not lock the other helpers out"
                over = await c.post("/client/media/compress-video")
                assert over.status_code == 429, "the video pool is still bounded"
            finally:
                release.set()
                await asyncio.gather(*held)
            assert mw.active == 0 and mw.pool_active["/client/media/compress-video"] == 0
    asyncio.run(run())
