"""`musicgeni` to the Nostr/Concord bot makes a SONG -- the video and the MP3 -- and `geni` is a word, not a substring.

2026-10-11: a member typed `musicgeni <a song>` to the PosterChan bot in a Concord room and got an IMAGE: every
dispatcher asked `"geni" in text`, and the bot had no musicgeni at all. The bot now runs the node's own
musicgeni (POST /api/media/song -- the same command the AI chat and Telegram run) and answers with ONE reply
carrying the branded MP4 and the song's MP3. The fediverse (Pleroma) bot still makes no songs.

The dispatch is driven through tests/test_every_bot_command.py's stubbed `bot` fixture (no network, no GPU);
tests/test_bot_runs_only_the_command_you_typed.py proves no OTHER word reaches the image generator.
"""
import json
import sys
from pathlib import Path

import pytest

from tests.test_every_bot_command import bot, _nostr, _room, _backends   # noqa: F401  (bot is a fixture)
import nostrListener as nl   # noqa: E402  (on sys.path via the import above)

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("where", ["nostr", "concord"])
def test_musicgeni_answers_with_the_video_and_the_mp3(bot, where):
    said = _nostr(bot, "musicgeni lofi rain | la la la") if where == "nostr" else _room(bot, "musicgeni lofi rain | la la la")[0]
    assert len(said) == 1, said
    text, kw = said[0]
    assert kw.get("video_bytes") and kw.get("audio_bytes", b"").startswith(b"ID3"), kw
    assert "MP3" in text and "lofi rain" in text, text
    call = [c for c in bot["calls"] if c[0] == "generate_song"]
    assert call and call[0][1][0] == "lofi rain | la la la", "the words after `musicgeni` were not what was sent"
    assert "geni" not in _backends(bot), "musicgeni drew a picture"


def test_a_song_is_one_per_person_per_cooldown(bot):
    _nostr(bot, "musicgeni lofi rain")
    again = _nostr(bot, "musicgeni something else")
    assert "wait" in again[0][0].lower(), again
    assert _backends(bot).count("generate_song") == 1


def test_one_song_at_a_time(bot):
    assert nl._music_busy.acquire(blocking=False)
    try:
        said = _nostr(bot, "musicgeni lofi rain")
    finally:
        nl._music_busy.release()
    assert "already making a song" in said[0][0], said
    assert "generate_song" not in _backends(bot)


def test_a_failed_song_says_so(bot):
    bot["set"]("generate_song", (None, None, None, "the GPU is busy"))
    said = _nostr(bot, "musicgeni lofi rain")
    assert "couldn't make that song" in said[0][0] and "GPU is busy" in said[0][0], said
    assert not said[0][1], said


def test_musicgeni_with_nothing_after_it_says_how(bot):
    said = _nostr(bot, "musicgeni")
    assert "usage" in said[0][0].lower() and "generate_song" not in _backends(bot), said


@pytest.mark.parametrize("where", ["nostr", "concord"])
@pytest.mark.parametrize("text", ["musicgeni", "videogeni a cat", "genius idea", "that is ingenious"])
def test_words_containing_geni_draw_no_picture(bot, where, text):
    _nostr(bot, text) if where == "nostr" else _room(bot, text)
    assert "geni" not in _backends(bot), f"{where}: {text!r} generated an image"


@pytest.mark.parametrize("text", ["geni a cat", "/geni a cat", "  GENI a cat"])
def test_geni_still_draws(bot, text):
    said = _nostr(bot, text)
    assert "geni" in _backends(bot) and said[0][1].get("image_bytes"), said


def test_a_reply_can_carry_a_video_and_an_mp3():
    import nostr as _mk
    assert _mk._to_media_list(None, b"v", b"a") == [(b"v", "video/mp4"), (b"a", "audio/mpeg")]
    assert _mk._to_media_list(None, None, b"a") == [(b"a", "audio/mpeg")]
    assert _mk._to_media_list(None, b"v", None) == [(b"v", "video/mp4")]


def test_the_image_prompt_keeps_every_word_but_the_command(monkeypatch):
    """`prompt.replace("geni", "")` turned "geni a genius cat" into "a us cat"."""
    import posterchanai_api as api
    sent = {}

    class _Resp:
        def read(self):
            return json.dumps({"error": "stop here"}).encode()

    def urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data)
        return _Resp()
    monkeypatch.setattr(api, "POSTERCHANAI_API_ENDPOINT", "http://node.test")
    monkeypatch.setattr(api.request, "urlopen", urlopen)
    api.generate_image_bytes("geni a genius cat")
    assert sent["body"]["prompt"] == "a genius cat", sent


def test_generate_song_reads_the_nodes_answer(monkeypatch):
    import base64
    import posterchanai_api as api
    sent = {}

    class _Resp:
        def read(self):
            return json.dumps({"ok": True, "content": "Generated song for: lofi", "video": base64.b64encode(b"mp4").decode(),
                               "mp3": base64.b64encode(b"ID3x").decode()}).encode()

    def urlopen(req, timeout=None):
        sent["url"], sent["body"] = req.full_url, json.loads(req.data)
        return _Resp()
    monkeypatch.setattr(api, "POSTERCHANAI_API_ENDPOINT", "http://node.test")
    monkeypatch.setattr(api.request, "urlopen", urlopen)
    assert api.generate_song("lofi | la") == ("Generated song for: lofi", b"mp4", b"ID3x", None)
    assert sent["url"].endswith("/api/media/song") and sent["body"] == {"prompt": "lofi | la"}


def test_the_fediverse_bot_makes_no_songs_and_draws_none_for_one():
    """The Pleroma listener is one long loop no test drives; what it decides with is checked here: the
    music words are answered with a sentence BEFORE the image branch, and that branch asks `is_geni`."""
    src = (ROOT / "botframework/pleromaListener.py").read_text()
    assert '"geni" in user_content' not in src
    i_music, i_geni = src.index('command_word(prompt_text, "musicgeni")'), src.index("elif is_geni(prompt_text):")
    assert i_music < i_geni
    assert "generate_song" not in src
