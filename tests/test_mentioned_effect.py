"""The `mentioned` effect: the "<THING> MENTIONED" meme -- an anime girl cheering over your picture, your word as
the caption ("add new effect for this 'mentioned' meme type", from a post captioned MICHIGAN MENTIONED).

Rendered for real (the shipped assets through the shipped ffmpeg path) and checked as the person gets it: a video
with sound, as long as the cheer, with her on the picture and the caption burned in -- and reachable from every
surface an effect lives on (chat command with its word, the media API, Telegram's button + reply prompt, the Meme
Builder's overlay clips).
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
FFMPEG = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")


def _frame(path, t):
    png = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-f", "image2pipe",
                          "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(png)).convert("RGB")


def _render(word):
    from app.services.effects_service import mentioned_attachments
    buf = io.BytesIO(); Image.new("RGB", (640, 480), (20, 60, 140)).save(buf, "JPEG")
    outs, summary = mentioned_attachments([("photo.jpg", buf.getvalue(), "image/jpeg")], word)
    assert outs, summary
    return outs[0], summary


@FFMPEG
def test_she_cheers_over_the_picture_with_the_caption_and_a_sound():
    o, summary = _render("michigan")
    assert o["content_type"] == "video/mp4" and o["filename"] == "photo_mentioned.mp4"
    assert "Michigan Mentioned" in summary
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(o["data"]); path = f.name
    try:
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
                                          "-of", "json", path], capture_output=True, text=True, check=True).stdout)
        assert {"video", "audio"} <= {s["codec_type"] for s in info["streams"]}, info
        assert 2.5 < float(info["format"]["duration"]) < 3.5, info["format"]
        im = _frame(path, 1.0).resize((64, 48))
        px = list(im.getdata())
        # She is on the picture: grey hoodie / skin / brown hair where there was only flat blue.
        her = sum(1 for r, g, b in px if not (b > r + 60 and b > g + 30))
        assert her > 64 * 48 * 0.15, ("she is not in the rendered video", her)
        # The caption is burned in ABOVE her, in its own band at the top -- drawn over her it hid her on small
        # pictures ("character went behind the text"). Counted at FULL size: letters are thin, and shrinking the
        # frame first averages them away.
        from app.services.effects_service.audio2 import MENTIONED_CAPTION_BAND, MENTIONED_GIRL_HEIGHT
        full = _frame(path, 1.0)
        band = list(full.crop((0, 0, full.width, int(full.height * MENTIONED_CAPTION_BAND))).getdata())
        white = sum(1 for r, g, b in band if r > 235 and g > 235 and b > 235)
        assert white > len(band) * 0.01, ("no caption in the top band", white, len(band))
        # ...and none of it reaches down to where she starts.
        girl_top = int(full.height * (1 - MENTIONED_GIRL_HEIGHT))
        below = list(full.crop((0, girl_top, full.width, full.height)).getdata())
        stray = sum(1 for r, g, b in below if r > 245 and g > 245 and b > 245)
        assert stray < len(below) * 0.002, ("caption text over the character", stray, len(below))
    finally:
        os.unlink(path)


def test_the_caption_is_the_word_then_mentioned():
    from app.services.effects_service import mentioned_caption
    assert mentioned_caption("michigan") == "MICHIGAN MENTIONED"
    assert mentioned_caption("  new   york ") == "NEW YORK MENTIONED"
    assert mentioned_caption("") == "", "there is no default: the person says what got mentioned"
    assert len(mentioned_caption("x" * 500)) <= 60 + len(" MENTIONED")


def test_mentioned_is_reachable_everywhere_an_effect_lives():
    from app.services.command_service.core import CommandService
    assert "mentioned" in CommandService.COMMANDS and "mentioned" in CommandService.ANIMATED_EFFECTS
    assert "mentioned" in CommandService.MOTION_EFFECTS and CommandService.wants_attachments("mentioned")
    src = (ROOT / "app/services/command_service/core.py").read_text()
    assert 'command == "mentioned":\n            return await self._mentioned_command(arg, attachments)' in src, \
        "the chat command does not hand the effect its word"
    assert '"mentioned"' in (ROOT / "app/routers/media_api.py").read_text()
    # Telegram no longer renders effects (2026-10-08): the word is answered with where it lives now.
    from app.routers.telegram import messages as tg
    assert "mentioned" in tg._TG_MOVED and "mentioned" in tg._TG_COMMANDS
    from app.services import meme_builder_service as mb
    assert any("mentioned" in json.dumps(c) for c in mb.alpha_effect_catalog()), \
        "the Meme Builder cannot add her as a layer"


def test_the_chat_command_passes_the_word_through_the_modifier_parser(monkeypatch):
    """`mentioned michigan zoom`: the trailing modifier is taken, the word reaches the effect."""
    from app.services.command_service import core
    seen = {}

    async def fake(self, arg, attachments):
        seen["arg"] = arg
        return {"type": "text", "content": "ok"}
    monkeypatch.setattr(core.CommandService, "_mentioned_command", fake, raising=False)
    svc = core.CommandService.__new__(core.CommandService)
    svc._effects_no_forward = True
    import asyncio
    try:
        asyncio.run(svc._execute_command_inner("mentioned", "michigan", None, None, [("a.jpg", b"x", "image/jpeg")], None))
    except Exception:
        pass
    assert seen.get("arg") == "michigan", seen


def test_the_sprite_is_committed_so_re_running_does_not_redraw_her():
    sprite = ROOT / "assets" / "mentioned_cheer_sprite.png"
    assert sprite.exists() and (ROOT / "assets" / "mentioned_cheer.mov").exists() and (ROOT / "assets" / "mentioned_cheer.mp3").exists()
    im = Image.open(sprite)
    assert im.mode == "RGBA" and im.getextrema()[3][0] == 0, "the sprite has no transparent background"


def test_the_media_api_hands_the_effect_only_the_word(monkeypatch):
    """Code review, 2026-10-07: /api/media/process (the fediverse bots' path) strips trailing modifiers, `meme <text>`
    and `char <name>` into `arg` -- but the `mentioned` branch read the raw req.arg, so `mentioned michigan zoom meme
    lol` captioned "MICHIGAN ZOOM MEME LOL MENTIONED" while also zooming and adding the meme text."""
    import asyncio
    import base64
    from app.routers import media_api
    from app.services import effects_service
    seen = {}

    def fake(attachments, word=""):
        seen["word"] = word
        return [], "stop here"
    monkeypatch.setattr(effects_service, "mentioned_attachments", fake)
    req = media_api.MediaProcessRequest(command="mentioned", arg="michigan zoom meme lol",
                                        media=[media_api.MediaItem(filename="a.jpg", data=base64.b64encode(b"x").decode(),
                                                                   content_type="image/jpeg")])
    try:
        asyncio.run(media_api.process_media(req, None, None, True))
    except Exception:
        pass
    assert seen.get("word") == "michigan", seen


def test_with_no_word_it_asks_for_one_instead_of_inventing_it():
    """"why does it default to posterchan mentioned? Users should add it" -- no word is a question back, on every
    surface: the effect, the chat command and the media API."""
    import asyncio
    import base64
    from app.services.effects_service import MENTIONED_ASK, mentioned_attachments
    outs, said = mentioned_attachments([("a.jpg", b"x", "image/jpeg")], "  ")
    assert outs == [] and said == MENTIONED_ASK
    from app.services.command_service.core import CommandService
    svc = CommandService.__new__(CommandService)
    got = asyncio.run(svc._mentioned_command("", [("a.jpg", b"x", "image/jpeg")]))
    assert got == {"type": "text", "content": MENTIONED_ASK}, got
    from app.routers import media_api
    req = media_api.MediaProcessRequest(command="mentioned", arg="zoom",
                                        media=[media_api.MediaItem(filename="a.jpg", data=base64.b64encode(b"x").decode(),
                                                                   content_type="image/jpeg")])
    r = asyncio.run(media_api.process_media(req, None, None, True))
    assert r == {"error": MENTIONED_ASK}, r
