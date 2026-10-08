"""The `nami` effect: Nami (One Piece) with money bags for pupils, rubbing her hands, over your image.

Rendered for real (the shipped assets through the shipped ffmpeg path) and checked as the person gets it: a
video with sound, about as long as the ka-ching, with her on the picture -- and reachable from every surface
an effect lives on (chat command, Telegram's effect keyboard, the Meme Builder's overlay clips).
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def _probe(data):
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(data); path = f.name
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json", path],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out), path
    except Exception:
        os.unlink(path); raise


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_nami_renders_a_video_with_her_and_the_ka_ching():
    from app.services.effects_service import nami_attachments
    buf = io.BytesIO(); Image.new("RGB", (640, 480), (20, 60, 140)).save(buf, "JPEG")
    outs, summary = nami_attachments([("photo.jpg", buf.getvalue(), "image/jpeg")])
    assert outs, summary
    o = outs[0]
    assert o["content_type"] == "video/mp4" and o["filename"] == "photo_nami.mp4"
    info, path = _probe(o["data"])
    try:
        kinds = {s["codec_type"] for s in info["streams"]}
        assert {"video", "audio"} <= kinds, kinds
        assert 3.0 < float(info["format"]["duration"]) < 4.5, info["format"]
        # She is ON the picture: a mid-video frame is no longer the plain blue photo (orange hair, skin, gold).
        frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.0", "-i", path, "-frames:v", "1", "-f", "image2pipe",
                                "-vcodec", "png", "-"], capture_output=True, check=True).stdout
        im = Image.open(io.BytesIO(frame)).convert("RGB").resize((64, 48))
        warm = sum(1 for r, g, b in im.getdata() if r > 150 and r > b + 60)
        assert warm > 64 * 48 * 0.05, ("Nami is not in the rendered video", warm)
    finally:
        os.unlink(path)


def test_nami_is_reachable_everywhere_an_effect_lives():
    from app.services.command_service.core import CommandService
    assert "nami" in CommandService.COMMANDS and "nami" in CommandService.ANIMATED_EFFECTS
    assert CommandService.wants_attachments("nami")
    # Telegram no longer renders effects (2026-10-08): the word is answered with where it lives now.
    from app.routers.telegram import messages as tg
    assert "nami" in tg._TG_MOVED and "nami" in tg._TG_COMMANDS
    from app.services import meme_builder_service as mb
    assert any(c.get("name") == "nami" or c.get("effect") == "nami" or "nami" in json.dumps(c) for c in mb.alpha_effect_catalog()), \
        "the Meme Builder cannot add Nami as a layer"


def test_the_sprite_is_committed_so_re_running_does_not_redraw_her():
    sprite = ROOT / "assets" / "nami_money_sprite.png"
    assert sprite.exists() and (ROOT / "assets" / "nami_money_sprite.png.json").exists()
    im = Image.open(sprite)
    assert im.mode == "RGBA" and im.getextrema()[3][0] == 0, "the sprite has no transparent background"
