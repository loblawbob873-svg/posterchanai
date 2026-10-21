"""Meme Builder as a video editor (2026-10-21: "we want it to be a real video editor").

Pinned: a timeline far past the old 120 s renders, TO A FILE (render(out_file=)) so a long result never sits in
memory, and its real duration is what the timeline said; the render's time budget grows with the length; a source
is streamed to disk with the size cap enforced while it arrives (an endless or lying body is cut off); a long
project is rendered on the node that was asked rather than forwarded (the forward buffers the answer in memory).
"""
import asyncio
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import HTTPException
from PIL import Image

from app.routers import client as C
from app.services import meme_builder_service as mb


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_WORK_DIR", str(tmp_path / "work"))
    return tmp_path


def test_a_long_timeline_renders_to_a_file_at_its_full_length(work):
    ff = mb.media_service.resolve_ffmpeg()
    if not ff:
        pytest.skip("ffmpeg is not available on this node")
    src = str(work / "a.png")
    Image.new("RGB", (64, 48), (200, 30, 30)).save(src)
    secs = 300.0                                               # 2.5x the old cap
    edit = {"w": 160, "h": 120, "fps": 5, "bg": "#000000", "duration": secs, "fmt": "mp4",
            "layers": [{"type": "image", "src": "S", "start": 0, "dur": secs, "x": 0, "y": 0, "w": 160, "h": 120,
                        "opacity": 1, "fit": "cover"}]}
    out = str(work / "out.mp4")
    path, ctype = mb.render(edit, {"S": src}, out)
    assert path == out and ctype == "video/mp4" and os.path.getsize(out) > 0
    probe = os.path.join(os.path.dirname(ff), "ffprobe")
    probe = probe if os.path.exists(probe) else "ffprobe"
    d = float(subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out],
                             capture_output=True, text=True).stdout.strip() or 0)
    assert abs(d - secs) < 1.5, d


def test_the_limits_are_an_editors():
    assert mb.MAX_DURATION >= 1800 and mb.MAX_SOURCE_BYTES >= 2 * 1024 ** 3
    assert mb.render_timeout(10) < mb.render_timeout(1800)
    assert mb.render_timeout(1800) >= 1800 * 4, "a 30-minute render must not be killed at the meme budget"
    js = open("static/js/client/meme.js").read()
    assert "const MAX_DUR = 1800;" in js
    assert "ZOOM_MIN = 0.003" in js, "a 30-minute edit must fit a phone's timeline"


def test_a_long_project_is_not_forwarded_to_a_peer():
    src = open(C.__file__).read()
    body = src[src.index("async def meme_render("):][:6000]
    assert "_fwd = None if _secs > 120 else await _meme_lb_forward(" in body


class _Endless(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        if self.path == "/declared":
            self.send_header("Content-Length", str(50 * 1024 * 1024))
        self.end_headers()
        try:
            for _ in range(50):
                self.wfile.write(b"\0" * (1024 * 1024))
        except Exception:
            pass

    def log_message(self, *a):
        pass


@pytest.mark.parametrize("path", ["/declared", "/undeclared"])
def test_a_source_past_the_cap_is_cut_off_on_disk(work, path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Endless)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = str(work / "src.mp4")
        with pytest.raises(HTTPException) as e:
            asyncio.run(C._fetch_media_to_file(f"http://127.0.0.1:{srv.server_port}{path}", {"127.0.0.1"}, out,
                                               3 * 1024 * 1024))
        assert e.value.status_code == 413
        assert not os.path.exists(out) or os.path.getsize(out) <= 4 * 1024 * 1024
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_source_under_the_cap_lands_whole(work):
    class _Small(_Endless):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 1000)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Small)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = str(work / "src.mp4")
        n = asyncio.run(C._fetch_media_to_file(f"http://127.0.0.1:{srv.server_port}/a.mp4", {"127.0.0.1"}, out, 4096))
        assert n == 1000 and open(out, "rb").read() == b"x" * 1000
    finally:
        srv.shutdown()
        srv.server_close()


def test_scratch_space_is_on_the_data_disk_not_tmpfs(work):
    from app.services import work_dir
    d = work_dir.mkdtemp("pcmemesrc-")
    assert d.startswith(str(work / "work")) and os.path.isdir(d)
    assert "work_dir.mkdtemp(\"pcmeme-\")" in open(mb.__file__).read()
