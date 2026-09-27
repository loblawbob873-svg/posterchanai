"""Talking replies: a Nostr bot answers with a short line spoken by a face, in a voice, in its personality.

Run: venv-unified/bin/python -m pytest tests/test_talking_reply_bot.py

Admin → Bots: tick "Talking replies", upload a face (the mouth is detected, then dragged into place),
upload a voice clip, and every reply the bot makes -- to a mention, or a random reply to a stranger on
the global feed -- is text from its Personality prompt, spoken in that voice by that face (the Meme
Builder's `talk`). The bot sends only text; app/services/talkbot_service.py renders with the bot's
OWN saved assets, behind a credential scoped to that one bot.

These tests run the real pieces wherever the machine has them: the real ffmpeg normalisation of a
voice clip, the real `add_talk` lip-sync on a generated face, and the real bot reply functions.
"""
import asyncio
import importlib
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

from app.services import talkbot_service as tb

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
BOTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "botframework")


def run(c):
    return asyncio.run(c)


def _wav(tmp_path, seconds=3):
    p = str(tmp_path / "voice.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=220:d={seconds}",
                    "-ar", "24000", "-ac", "1", p], check=True, timeout=60)
    with open(p, "rb") as f:
        return f.read()


def _face_png():
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (320, 320), (230, 190, 160))
    d = ImageDraw.Draw(im)
    d.ellipse((60, 40, 260, 280), fill=(240, 200, 170))
    d.ellipse((115, 120, 145, 150), fill=(40, 40, 40))
    d.ellipse((175, 120, 205, 150), fill=(40, 40, 40))
    d.line((130, 215, 190, 215), fill=(120, 30, 40), width=6)
    b = io.BytesIO()
    im.save(b, format="PNG")
    return b.getvalue()


# ---- the rules ------------------------------------------------------------------------------------

def test_each_bot_gets_its_own_credential():
    assert tb.token("alice") != tb.token("bob")
    assert tb.token("alice") == tb.token("alice"), "must be stable across restarts"
    assert tb.token_ok("alice", tb.token("alice"))
    assert not tb.token_ok("alice", tb.token("bob")), "a bot must not render as another bot"
    assert not tb.token_ok("alice", "") and not tb.token_ok("", tb.token(""))


def test_a_mouth_placement_is_clamped():
    m = tb.clean_mouth({"x": 3, "y": -1, "w": 9, "angle": 400, "anime": 1})
    assert m == {"x": 1.0, "y": 0.0, "w": 0.6, "angle": 45.0, "anime": True}
    assert tb.clean_mouth(json.dumps({"x": 0.4, "y": 0.7, "w": 0.1})) ["x"] == 0.4, "the form stores JSON"
    assert tb.clean_mouth("nonsense") is None and tb.clean_mouth(None) is None


def test_what_is_spoken_has_no_links_and_is_short():
    t = tb.clean_text("gm #nostr see https://x.example/a nostr:npub1abc and npub1qqqq ok")
    assert "http" not in t and "npub1" not in t and "nostr:" not in t and "nostr" in t
    assert len(tb.clean_text("word " * 200)) <= tb.MAX_CHARS


# ---- the bot's endpoint ----------------------------------------------------------------------------

def _client(monkeypatch, cfg, rendered):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.routers import bot_talk
    app = FastAPI()
    app.include_router(bot_talk.router)
    app.dependency_overrides[get_db] = lambda: None

    def bot_config(db, name):
        if name not in cfg:
            raise LookupError(name)
        return cfg[name]

    async def render(db, face, voice, mouth, text):
        rendered.append((face, voice, mouth, text))
        return b"\x00\x00\x00\x18ftypmp42clip"
    monkeypatch.setattr(tb, "bot_config", bot_config)
    monkeypatch.setattr(tb, "render", render)
    return TestClient(app)


def _faces(*shas):
    return json.dumps([{"sha": s, "mouth": {"x": 0.1 * (i + 1), "y": 0.6, "w": 0.1}} for i, s in enumerate(shas)])


def test_the_bot_endpoint_renders_only_with_that_bots_own_assets(monkeypatch):
    cfg = {"alice": {"talk_faces": _faces("f" * 64), "talk_voice_sha": "v" * 64},
           "bob": {"talk_faces": _faces("b" * 64), "talk_voice_sha": "c" * 64}}
    rendered = []
    with _client(monkeypatch, cfg, rendered) as c:
        ok = c.post("/api/bots/talk", json={"bot": "alice", "text": "hi"},
                    headers={"X-PC-Talk-Token": tb.token("alice")})
        assert ok.status_code == 200 and ok.headers["content-type"] == "video/mp4"
        assert rendered[0][0] == "f" * 64 and rendered[0][1] == "v" * 64 and rendered[0][3] == "hi"
        assert rendered[0][2]["x"] == pytest.approx(0.1), "the face's OWN mouth placement must be used"
        stolen = c.post("/api/bots/talk", json={"bot": "bob", "text": "hi"},
                        headers={"X-PC-Talk-Token": tb.token("alice")})
        assert stolen.status_code == 401, "alice's credential rendered as bob"
        assert c.post("/api/bots/talk", json={"bot": "alice", "text": "hi"}).status_code == 401


def test_each_reply_picks_one_of_up_to_three_faces_with_its_own_mouth(monkeypatch):
    shas = ["1" * 64, "2" * 64, "3" * 64]
    cfg = {"alice": {"talk_faces": _faces(*shas, "4" * 64), "talk_voice_sha": "v" * 64}}
    rendered = []
    with _client(monkeypatch, cfg, rendered) as c:
        for _ in range(60):
            c.post("/api/bots/talk", json={"bot": "alice", "text": "hi"}, headers={"X-PC-Talk-Token": tb.token("alice")})
    used = {r[0] for r in rendered}
    assert used == set(shas), f"expected all three faces over 60 replies and never a 4th, got {used}"
    for face, _v, mouth, _t in rendered:
        assert mouth["x"] == pytest.approx(0.1 * (shas.index(face) + 1)), "a face was drawn with another face's mouth"


def test_faces_come_in_shuffled_rounds_never_twice_in_a_row():
    """Every face once per round, in random order; never the same face on consecutive replies --
    a plain random pick showed fever the same picture three times running."""
    from app.routers import bot_talk
    faces = [{"sha": c * 64, "mouth": None} for c in "123"]
    picks = [bot_talk.next_face("bag-test", faces)[0] for _ in range(300)]
    assert all(a != b for a, b in zip(picks, picks[1:])), "the same face twice in a row"
    for r in range(0, 300, 3):
        assert sorted(picks[r:r + 3]) == [0, 1, 2], f"round {r // 3} did not use every face once"
    assert len({tuple(picks[r:r + 3]) for r in range(0, 300, 3)}) > 1, "the order never changes"
    one = [{"sha": "9" * 64, "mouth": None}]
    assert [bot_talk.next_face("solo", one)[0] for _ in range(3)] == [0, 0, 0]


def test_faces_are_validated():
    got = tb.faces_of({"talk_faces": json.dumps([{"sha": "a" * 64}, {"sha": "nope"}, "junk", {"sha": "b" * 64,
                                                  "mouth": {"x": 9}}])})
    assert [f["sha"] for f in got] == ["a" * 64, "b" * 64] and got[1]["mouth"]["x"] == 1.0
    assert tb.faces_of({}) == [] and tb.faces_of({"talk_faces": "not json"}) == []


def test_a_bot_without_a_face_or_voice_is_told_so(monkeypatch):
    rendered = []
    with _client(monkeypatch, {"carol": {"talk_faces": _faces("f" * 64)}}, rendered) as c:
        r = c.post("/api/bots/talk", json={"bot": "carol", "text": "hi"},
                   headers={"X-PC-Talk-Token": tb.token("carol")})
    assert r.status_code == 409 and rendered == []


# ---- the real pieces ------------------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg")
def test_a_reply_renders_as_a_talking_clip_with_sound(monkeypatch, tmp_path):
    """The real add_talk on a real picture and a real WAV; only the voice MODEL is stood in for."""
    face, voice = _face_png(), _wav(tmp_path)
    blobs = {"f" * 64: face, "v" * 64: voice}

    async def read_blob(db, sha):
        return blobs[sha]

    async def generate_voice(db, text, reference, reference_path=None):
        assert reference == voice, "the bot's own voice clip must be the reference"
        # Without a FILE the local path refuses ("no local copy of the reference clip") and every
        # render went to another node -- this node's GPU never spoke.
        assert reference_path and open(reference_path, "rb").read() == voice, "no local reference file"
        return _wav(tmp_path, 2), "local"
    from app.services import voice_factory
    monkeypatch.setattr(tb, "_read_blob", read_blob)
    monkeypatch.setattr(voice_factory, "generate_voice", generate_voice)
    clip = run(tb.render(None, "f" * 64, "v" * 64, {"x": 0.5, "y": 0.67, "w": 0.2}, "Hello there!"))
    out = tmp_path / "clip.mp4"
    out.write_bytes(clip)
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                              str(out)], capture_output=True, text=True).stdout.split()
    assert "video" in streams and "audio" in streams, streams


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg")
def test_uploading_a_voice_normalises_it_and_refuses_a_useless_clip(monkeypatch, tmp_path):
    saved = []

    async def save_blob(db, pubkey, data, mime, **kw):
        saved.append((pubkey, mime, kw.get("keep"), data[:4]))
        return {"sha256": "a" * 64}
    from app.services import blossom_service
    monkeypatch.setattr(blossom_service, "save_blob", save_blob)
    monkeypatch.setattr(tb, "_bot_pubkey", lambda nsec: "pk")
    # a 44.1kHz STEREO mp3-ish input comes out as the model's mono 24k WAV
    src = str(tmp_path / "in.mp3")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=4", "-ac", "2",
                    "-ar", "44100", src], check=True, timeout=60)
    got = run(tb.save_voice(None, "nsec", open(src, "rb").read(), "in.mp3"))
    assert got["sha"] == "a" * 64 and 3.5 <= got["seconds"] <= 4.5
    assert saved == [("pk", "audio/wav", True, b"RIFF")], "stored as WAV, owned by the bot, never swept"
    with pytest.raises(ValueError):
        run(tb.save_voice(None, "nsec", _wav(tmp_path, 1), "short.wav"))
    with pytest.raises(ValueError):
        run(tb.save_voice(None, "nsec", b"not audio at all", "x.wav"))


def test_uploading_a_face_keeps_it_and_says_where_the_mouth_is(monkeypatch):
    saved = []

    async def save_blob(db, pubkey, data, mime, **kw):
        saved.append((mime, kw.get("keep")))
        return {"sha256": "e" * 64}
    from app.services import blossom_service
    monkeypatch.setattr(blossom_service, "save_blob", save_blob)
    monkeypatch.setattr(tb, "_bot_pubkey", lambda nsec: "pk")
    got = run(tb.save_face(None, "nsec", _face_png()))
    assert got["sha"] == "e" * 64 and saved == [("image/png", True)]
    assert set(got["mouth"]) >= {"found", "x", "y", "w"}, "the form needs a starting spot for the marker"
    with pytest.raises(ValueError):
        run(tb.save_face(None, "nsec", b"no picture here"))


# ---- the manager ------------------------------------------------------------------------------------

def test_the_manager_turns_talking_on_only_when_it_is_fully_set_up(monkeypatch):
    from app.services import bot_manager_service as bm, settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: d)
    base = {"name": "talky", "platform": "nostr", "bot_type": "text", "modes": ["--nostr"],
            "nostr_nsec": "11" * 32, "talk_enabled": True, "talk_faces": _faces("f" * 64),
            "talk_voice_sha": "v" * 64, "talk_max_words": "12"}
    env = bm._build_env(dict(base), {})
    assert env.get("NOSTR_TALK") == "1" and env.get("NOSTR_TALK_BOT") == "talky"
    assert env.get("NOSTR_TALK_TOKEN") == tb.token("talky") and env.get("NOSTR_TALK_MAX_WORDS") == "12"
    for missing in ("talk_enabled", "talk_faces", "talk_voice_sha"):
        env = bm._build_env({k: v for k, v in base.items() if k != missing}, {})
        assert "NOSTR_TALK" not in env, f"talking turned on without {missing}"


# ---- the bot ------------------------------------------------------------------------------------------

@pytest.fixture
def listener(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(BOTS)
    monkeypatch.setenv("NOSTR_NSEC", "11" * 32)
    monkeypatch.setenv("NOSTR_TALK", "1")
    monkeypatch.setenv("NOSTR_TALK_BOT", "talky")
    monkeypatch.setenv("NOSTR_TALK_TOKEN", "tok")
    monkeypatch.setenv("NOSTR_TALK_MAX_WORDS", "9")
    sys.modules.pop("nostrListener", None)
    mod = importlib.import_module("nostrListener")
    monkeypatch.setattr(mod, "_RR_AUTHORS_FILE", str(tmp_path / "authors.json"))
    return mod


def test_a_talking_reply_carries_the_clip(listener, monkeypatch):
    sent = []
    monkeypatch.setattr(listener, "_talk_clip", lambda text: b"CLIP")
    listener._send_spoken(lambda t, **m: sent.append((t, m)), "hello")
    assert sent == [("", {"video_bytes": b"CLIP"})], "a talking reply posts the video alone, no text"


def test_a_failed_render_still_answers_in_text(listener, monkeypatch):
    sent = []
    monkeypatch.setattr(listener, "_talk_clip", lambda text: None)
    listener._send_spoken(lambda t, **m: sent.append((t, m)), "hello")
    assert sent == [("hello", {})], "a busy GPU must not leave a person without an answer"


def test_the_prompt_asks_for_a_short_spoken_line(listener):
    p = listener._talk_prompt("what's up?")
    assert p.startswith("what's up?") and "at most 9 words" in p
    low = p.lower()
    assert "talking" not in low and "video" not in low and "spoken" not in low, \
        "telling the model it is a talking picture made it narrate that instead of answering"


def test_a_models_preamble_is_not_said_or_posted(listener):
    real = 'Here\'s a talking head of Jonny Fever:\n\n"Listen here, you little shit. I don\'t know what that means'
    assert listener._spoken_line(real) == "Listen here, you little shit. I don't know what that means"
    assert listener._spoken_line('Here is my reply: "Well Well Well, look who it is."') == "Well Well Well, look who it is."
    for kept in ("Here's the thing: rates went up.", "Yeah, rates go up and people panic.", '"Nope," he said.'):
        assert listener._spoken_line(kept) == kept, kept


def test_the_cleaned_line_is_what_is_rendered_and_posted(listener, monkeypatch):
    sent, rendered = [], []
    monkeypatch.setattr(listener, "_talk_clip", lambda text: rendered.append(text) or b"CLIP")
    listener._send_spoken(lambda t, **m: sent.append(t), 'Here\'s a talking head:\n"Hi there."')
    assert rendered == ["Hi there."] and sent == [""], "the cleaned line is spoken; the post is the video"


def test_the_clip_is_asked_for_as_this_bot_with_its_credential(listener, monkeypatch):
    seen = {}

    class _Resp:
        status = 200
        headers = {"Content-Type": "video/mp4"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"MP4"

    def urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        seen["tok"] = req.headers.get("X-pc-talk-token")
        return _Resp()
    from urllib import request as rq
    monkeypatch.setattr(rq, "urlopen", urlopen)
    assert listener._talk_clip("hi") == b"MP4"
    assert seen["url"].endswith("/api/bots/talk") and seen["body"] == {"bot": "talky", "text": "hi"}
    assert seen["tok"] == "tok"


def test_random_replies_skip_whoever_muted_the_bot(listener, monkeypatch):
    own = "0" * 64

    def relay(lists):
        async def query(relays, filters, **kw):
            return lists
        return query
    monkeypatch.setattr(listener._nk._svc.relay, "query", relay([{"created_at": 2, "tags": [["p", own]]}]))
    assert listener._muted_us("a" * 64, own)
    monkeypatch.setattr(listener._nk._svc.relay, "query", relay([{"created_at": 2, "tags": [["p", "b" * 64]]}]))
    assert not listener._muted_us("a" * 64, own)

    async def down(relays, filters, **kw):
        raise ConnectionError("relay down")
    monkeypatch.setattr(listener._nk._svc.relay, "query", down)
    assert listener._muted_us("a" * 64, own), "could-not-ask must not mean 'reply anyway'"


def test_random_replies_reach_each_person_once_a_day(listener, monkeypatch):
    listener._rr_note_author("a" * 64)
    assert "a" * 64 in listener._rr_recent_authors()
    later = listener.time.time() + listener._RR_AUTHOR_GAP + 5
    monkeypatch.setattr(listener.time, "time", lambda: later)
    assert "a" * 64 not in listener._rr_recent_authors(), "a day later they may be answered again"
