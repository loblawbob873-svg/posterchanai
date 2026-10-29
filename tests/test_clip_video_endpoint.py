"""/client/media/clip-video: the Screen Recorder's Trim, through the REAL endpoint with real ffmpeg.

A browser records WebM; the recorder sends it with a start/end and gets back an MP4 of exactly that span (so it
plays on iOS, which has no WebM). Checked by measuring the result with ffprobe, not by its status code.
"""
import json
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import client as C
from app.services import media_service as M

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("VIDEO_ENCODER", "libx264")
    monkeypatch.setattr(M, "_video_encoder_cache", None)
    monkeypatch.setattr(C, "_compress_busy", [0])


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    """What MediaRecorder hands over: a 6-second VP8 WebM of a screen."""
    p = str(tmp_path_factory.mktemp("rec") / "screen.webm")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "6",
                    "-c:v", "libvpx", "-deadline", "realtime", "-b:v", "1M", p], check=True, timeout=180)
    return open(p, "rb").read()


def _client():
    app = FastAPI()
    app.include_router(C.router)
    return TestClient(app)


def _probe(data, tmp_path):
    p = tmp_path / "out.mp4"
    p.write_bytes(data)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_name",
                          "-of", "json", str(p)], capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout)


def test_a_span_comes_back_as_an_mp4_of_that_length(recording, tmp_path):
    with _client() as tc:
        r = tc.post("/client/media/clip-video", files={"file": ("screen.webm", recording, "video/webm")},
                    data={"start": "1.5", "end": "4"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    info = _probe(r.content, tmp_path)
    assert "mp4" in info["format"]["format_name"], info
    assert abs(float(info["format"]["duration"]) - 2.5) < 0.4, info
    assert any(s["codec_name"] == "h264" for s in info["streams"]), info


def test_no_end_converts_the_whole_recording(recording, tmp_path):
    with _client() as tc:
        r = tc.post("/client/media/clip-video", files={"file": ("screen.webm", recording, "video/webm")},
                    data={"start": "0"})
    assert r.status_code == 200, r.text
    assert abs(float(_probe(r.content, tmp_path)["format"]["duration"]) - 6) < 0.4


@pytest.mark.parametrize("start,end", [("3", "2"), ("-1", "2"), ("2", "2")])
def test_an_impossible_span_is_refused_before_any_work(recording, start, end, monkeypatch):
    monkeypatch.setattr(M, "clip_video_file", lambda *a, **k: pytest.fail("encoded an impossible span"))
    with _client() as tc:
        r = tc.post("/client/media/clip-video", files={"file": ("screen.webm", recording, "video/webm")},
                    data={"start": start, "end": end})
    assert r.status_code == 400, r.text


def test_a_file_that_is_not_a_video_is_an_error_not_an_empty_mp4():
    with _client() as tc:
        r = tc.post("/client/media/clip-video", files={"file": ("screen.webm", b"not a video at all", "video/webm")},
                    data={"start": "0", "end": "1"})
    assert r.status_code == 422 and "could not be trimmed" in r.json()["error"], r.text


def test_the_bytes_api_and_the_path_api_agree(recording, tmp_path):
    """clip_video (bytes, used by the `clip` command) now runs through clip_video_file -- one encoder ladder."""
    out = M.clip_video(recording, "screen.webm", 1, 3)
    assert abs(float(_probe(out, tmp_path)["format"]["duration"]) - 2) < 0.4


def test_the_anonymous_helper_bounds_cover_it():
    """An ffmpeg encode open to anonymous callers must sit behind PublicWorkMiddleware's size cap and its own
    small pool, or two long recordings lock translate/narrate/stt out of the node (and any size spools to RAM)."""
    from app.middleware.public_work import PublicWorkMiddleware as P
    assert P.LIMITS['/client/media/clip-video'] >= C._COMPRESS_VIDEO_MAX
    assert P.POOLS['/client/media/clip-video'] <= 2
