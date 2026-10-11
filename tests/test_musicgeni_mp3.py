"""A song made with `musicgeni` reaches the member as an MP3 too, not only as the branded MP4.

Asked by a member (2026-10-11): "can I download the MP3 of a song I generated?" The song is delivered as a
branded MP4 (the song over a PosterChan card). These run the REAL pipeline -- ACE-Step stubbed with a real
WAV, the real `make_music_video`, the real ffmpeg -- and check what each surface hands over:

  * AI chat: the persisted reply carries the `song` marker (that is what puts "Convert to MP3" on it after
    a reload), and the conversion that button runs (`extractaudio` on that very MP4) persists a real MP3
    under the song's name. The client half is tests/client/test_generated_song_to_mp3_full_app.py.
  * Telegram: the song arrives as the video AND as an MP3 audio file -- before, only the video was sent,
    and the bot has no conversion button to fall back on.
"""
import ast
import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="ffmpeg/ffprobe required")

REPO = Path(__file__).resolve().parents[1]


def _wav(seconds=2):
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                           f"sine=frequency=440:duration={seconds}", "-f", "wav", "pipe:1"],
                          capture_output=True, check=True).stdout


def _streams(data, suffix):
    fd, p = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", p],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)
    finally:
        os.unlink(p)


def _musicgeni(monkeypatch, command="musicgeni"):
    from app.services.command_service import gen
    from app.services.command_service.gen import _GenMixin
    from app.services import settings_store
    wav = _wav()

    async def make(db, prompt, lyrics):
        return wav, "wav"
    monkeypatch.setattr(gen.music_factory, "generate_music_for_user", make)
    real_get = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: "false" if k == "music_watermark_enabled" else real_get(k, d))
    cs = object.__new__(_GenMixin)
    cs.db = None
    cs.chat_service = types.SimpleNamespace(chat=None, num_predict=512)
    return asyncio.run(cs._musicgeni_command("lofi beat about rain | la la la"))


def test_ai_chat_song_persists_as_a_song_and_converts_to_a_real_mp3(monkeypatch):
    from app.routers import chat
    from app.services.command_service.media import _MediaMixin
    res = _musicgeni(monkeypatch)
    assert res["type"] == "generated_video" and res.get("has_audio"), res.get("type")
    mp4 = base64.b64decode(res["video"])
    assert any(s["codec_type"] == "audio" for s in _streams(mp4, ".mp4")["streams"])

    saved = {}

    async def save(user_id, conv_id, data, ext):
        name = f"enc_{len(saved)}.{ext}"
        saved[name] = data
        return f"vyram/chat/{conv_id}/{name}"
    monkeypatch.setattr(chat, "_save_artifact_blossom", save)
    monkeypatch.setattr(chat.chat_store, "enabled", lambda db: True)
    user = types.SimpleNamespace(id=7, username="vyram")

    content, _img, live = asyncio.run(chat.normalize_command_result(None, user, 5, dict(res), None))
    assert "!video[song](/api/files/vyram/5/enc_0.mp4)" in content, content

    # What "Convert to MP3" sends: `extractaudio` with that MP4 attached under the song's name.
    mx = object.__new__(_MediaMixin)
    out = asyncio.run(mx._extractaudio_command([("posterchan-song-2026-10-11-080600.mp4", mp4, "video/mp4")]))
    assert out["type"] == "files", out
    content, _img, live = asyncio.run(chat.normalize_command_result(None, user, 5, out, None))
    assert "!audio[posterchan-song-2026-10-11-080600.mp3](/api/files/vyram/5/enc_1.mp3)" in content, content
    probe = _streams(saved["enc_1.mp3"], ".mp3")
    assert probe["format"]["format_name"] == "mp3", probe["format"]
    assert [s["codec_name"] for s in probe["streams"]] == ["mp3"], probe["streams"]


class _FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send_video(self, chat_id, path, caption=None, **kw):
        self.sent.append(("video", Path(path).read_bytes(), caption, None))
        return {"ok": True}

    async def send_audio(self, chat_id, path, title=None, performer=None, duration=None, caption=None):
        self.sent.append(("audio", Path(path).read_bytes(), caption, title))
        return {"ok": True}

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("text", text, None, None))
        return {"ok": True}


def test_telegram_sends_the_song_as_video_and_as_mp3(monkeypatch):
    from app.routers.telegram import senders
    res = _musicgeni(monkeypatch)
    tg = _FakeTelegram()
    monkeypatch.setattr(senders, "telegram_service", tg)
    asyncio.run(senders._send_generated_video("42", res, res["content"]))
    kinds = [k for k, *_ in tg.sent]
    assert kinds == ["video", "audio"], tg.sent
    _k, mp3, caption, title = tg.sent[1]
    probe = _streams(mp3, ".mp3")
    assert probe["format"]["format_name"] == "mp3" and probe["streams"][0]["codec_name"] == "mp3"
    assert title and title.startswith("posterchan-song-") and title.endswith(".mp3"), title
    assert "MP3" in (caption or ""), caption


def test_a_pinned_rerun_delivers_the_mp3_too(monkeypatch):
    from app.routers.telegram import senders
    res = _musicgeni(monkeypatch)
    tg = _FakeTelegram()
    monkeypatch.setattr(senders, "telegram_service", tg)
    asyncio.run(senders._deliver_pin_result("42", res))
    assert [k for k, *_ in tg.sent] == ["video", "audio"], [k for k, *_ in tg.sent]


def test_a_silent_video_gets_no_mp3(monkeypatch):
    """videogeni's clip has no audio, and narration is not a song: only the video goes."""
    from app.routers.telegram import senders
    tg = _FakeTelegram()
    monkeypatch.setattr(senders, "telegram_service", tg)
    clip = {"type": "generated_video", "video": base64.b64encode(b"\x00\x00\x00\x18ftypmp42").decode(),
            "content": "a cat"}
    asyncio.run(senders._send_generated_video("42", clip, "a cat"))
    assert [k for k, *_ in tg.sent] == ["video"], tg.sent


def test_the_bots_song_endpoint_returns_the_video_and_a_real_mp3(monkeypatch):
    """POST /api/media/song -- what the Nostr/Concord bot's `musicgeni` calls -- runs the same command and
    hands back the branded MP4 AND the song as an MP3 (tests/test_bot_musicgeni.py drives the bot side)."""
    from app.routers import media_api
    _musicgeni(monkeypatch)        # installs the ACE-Step stub + settings for the command below
    out = asyncio.run(media_api.generate_song(media_api.SongRequest(prompt="lofi beat about rain | la la la"),
                                              db=None, _auth=True))
    assert out["ok"], out
    assert "lofi beat about rain" in out["content"]
    video = _streams(base64.b64decode(out["video"]), ".mp4")
    assert any(s["codec_type"] == "video" for s in video["streams"])
    probe = _streams(base64.b64decode(out["mp3"]), ".mp3")
    assert probe["format"]["format_name"] == "mp3" and [s["codec_name"] for s in probe["streams"]] == ["mp3"]


def test_the_song_endpoint_refuses_an_empty_request():
    from app.routers import media_api
    out = asyncio.run(media_api.generate_song(media_api.SongRequest(prompt="  "), db=None, _auth=True))
    assert out == {"ok": False, "error": "Usage: musicgeni <style> [| lyrics]"}


def test_the_chat_handler_delivers_videos_through_the_shared_sender():
    """The live Telegram chat path must use the same sender the test above drives -- it used to carry its
    own copy of the video send, which is how one path could gain the MP3 while the other did not."""
    tree = ast.parse((REPO / "app/routers/telegram/messages.py").read_text())
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_send_generated_video" in calls
    sends = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "send_video"]
    assert not sends, "messages.py sends a video itself instead of through _send_generated_video"
