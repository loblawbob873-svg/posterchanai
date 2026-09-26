"""A video posted to Social comes back SMALLER than the one the phone wrote.

Run: venv-unified/bin/python -m pytest tests/test_social_video_is_compressed.py

The composer hands every clip over 2 MB to /client/media/compress-video before uploading it to
Blossom. The encode there was constant-quality with NO bitrate ceiling (VAAPI `-qp 28`, x264
`-crf 28`), so its size tracked how detailed the picture is, not how big the source was — and a
grainy or detailed clip that the phone had already encoded efficiently came out BIGGER. Measured in
production on the Arc: 9,224,061 -> 17,278,953 bytes. The endpoint rightly refuses to hand back a
bigger file (204), so that post went up at full size: the feature ran and compressed nothing.

The social endpoint now passes a ceiling — 4 Mbps, or 60% of the source's own video bitrate,
whichever is lower — spelled per encoder as the live-stream clamp measured them. It is OPT-IN on
compress_video_file: the `compress` command and the stream-VOD path keep their encode unchanged.

These tests RUN FFMPEG on a clip built to have the failing shape (proven below to inflate uncapped),
because the old command was perfectly well-formed; it described the wrong thing.
"""
import os
import shutil
import subprocess

import pytest

from app.services import media_service as M

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe are what this measures")


@pytest.fixture(autouse=True)
def _cpu_encoder(monkeypatch):
    """libx264 on every box, so the numbers do not depend on which GPU the runner has."""
    monkeypatch.setenv("VIDEO_ENCODER", "libx264")
    monkeypatch.setattr(M, "_video_encoder_cache", None)


def _phone_clip(path):
    """A detailed, grainy clip the 'phone' already encoded tightly — the shape that inflated."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=1280x720:rate=30,noise=alls=14:allf=t", "-f", "lavfi", "-i", "sine=f=440",
         "-t", "8", "-c:v", "libx264", "-preset", "slow", "-b:v", "1500k", "-maxrate", "1500k",
         "-bufsize", "3000k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", path],
        check=True, timeout=180)
    return path


def _video_kbps(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=bit_rate", "-of", "csv=p=0", path], capture_output=True, text=True)
    return int(out.stdout.strip()) / 1000


def test_the_fixture_inflates_without_a_ceiling(tmp_path):
    """Otherwise the capped test below could pass on a clip that never had the problem."""
    src = _phone_clip(str(tmp_path / "phone.mp4"))
    out = M.compress_video_file(src, str(tmp_path / "plain.mp4"))
    assert os.path.getsize(out) > os.path.getsize(src), \
        "fixture no longer reproduces the bug: an uncapped encode must come out BIGGER"


def test_a_posted_clip_comes_back_smaller(tmp_path):
    src = _phone_clip(str(tmp_path / "phone.mp4"))
    cap = M.social_video_ceiling_kbps(src)
    assert cap < 1500, f"the ceiling must sit below the source's own rate, got {cap}k"
    out = M.compress_video_file(src, str(tmp_path / "posted.mp4"), max_kbps=cap)
    assert os.path.getsize(out) < os.path.getsize(src) * 0.85, \
        f"{os.path.getsize(src):,} -> {os.path.getsize(out):,}: the post would go up uncompressed"
    assert _video_kbps(out) <= cap * 1.15, f"video bitrate {_video_kbps(out):.0f}k over the {cap}k ceiling"


def test_the_ceiling_is_4mbps_for_a_heavy_source(tmp_path):
    src = str(tmp_path / "heavy.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=30,noise=alls=14:allf=t", "-t", "2", "-c:v", "libx264",
                    "-preset", "ultrafast", "-b:v", "16M", "-maxrate", "16M", "-bufsize", "32M",
                    "-pix_fmt", "yuv420p", src], check=True, timeout=120)
    assert M.social_video_ceiling_kbps(src) == M.SOCIAL_VIDEO_MAX_KBPS


def test_every_encoder_spells_a_ceiling_and_the_default_is_unchanged(tmp_path, monkeypatch):
    """Per encoder, a ceiling must be a CEILING (the clamp's measured spellings) — and without
    max_kbps the command is what the `compress` command and stream VODs always ran."""
    monkeypatch.setattr(M, "_probe_color", lambda ff, p: {})
    monkeypatch.setattr(M, "_render_node", lambda: "/dev/dri/renderD128")
    a = lambda enc, **k: " ".join(M._video_encode_cmd("ffmpeg", enc, "in.mp4", "out.mp4", "scale", 28, "fast", **k))
    assert "-rc vbr -cq 28 -b:v 0 -maxrate 1000k -bufsize 2000k" in a("h264_nvenc", max_kbps=1000)
    assert "-rc_mode VBR -b:v 1000k -maxrate 1000k -bufsize 2000k" in a("h264_vaapi", max_kbps=1000)
    assert "-crf 28 -maxrate 1000k -bufsize 2000k" in a("libx264", max_kbps=1000)
    for enc, plain in (("h264_nvenc", "-c:v h264_nvenc -preset p5 -cq 28 -pix_fmt"),
                       ("h264_vaapi", "-c:v h264_vaapi -qp 28 -colorspace"),
                       ("libx264", "-c:v libx264 -preset fast -crf 28 -pix_fmt")):
        assert plain in a(enc) and "maxrate" not in a(enc), f"{enc}: the uncapped default changed"


def test_the_social_endpoint_passes_the_ceiling():
    with open(os.path.join(ROOT, "app", "routers", "client.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("async def client_compress_video"):]
    body = body[:body.index("\n@router.")]
    assert "social_video_ceiling_kbps" in body and "max_kbps=" in body, \
        "/client/media/compress-video must encode under a ceiling or a detailed clip inflates"


def test_the_endpoint_hands_back_a_smaller_video(tmp_path):
    """What the composer actually sees: POST the clip, get a SMALLER mp4 back — not the 204 "keep
    your original" that production answered for the 9.2 MB clip."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import client as client_router

    app = FastAPI()
    app.include_router(client_router.router)
    src = _phone_clip(str(tmp_path / "phone.mp4"))
    with open(src, "rb") as fh, TestClient(app) as tc:
        r = tc.post("/client/media/compress-video", files={"file": ("phone.mp4", fh, "video/mp4")})
    assert r.status_code == 200, f"{r.status_code}: the post would go up uncompressed"
    assert r.headers["content-type"] == "video/mp4"
    assert len(r.content) < os.path.getsize(src) * 0.85, (os.path.getsize(src), len(r.content))
