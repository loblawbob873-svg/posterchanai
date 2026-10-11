"""A big voice sample is a usable voice sample: the byte cap is about SIZE, says so, and sits far above a phone video.

Run: venv-unified/bin/python -m pytest tests/test_voice_reference_size.py

Reported by a member on the Android APK: a ONE-WORD line was refused with "that reference clip is too
long". /client/voice/speak refused any upload over 16 MB before the step that trims it to
`voice_max_ref_seconds` (30 s) mono 24 kHz -- and a saved voice is very often a phone VIDEO (the studio
accepts video/*), which is 16 MB within seconds. The clip was perfectly usable, the sentence was about
LENGTH while the check was about BYTES, and it had nothing to do with the line typed.

These run with NO ffmpeg (they are on the gate's required list, and the CI runner has none): a stub
ffmpeg records its arguments and writes a short WAV, which is exactly the contract the endpoints rely on.
The real-ffmpeg version of the trim is tests/test_voice_reference_real_ffmpeg.py (not required: it
needs ffmpeg).
"""
import io
import json
import sys
import wave
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.routers import client as client_router
from app.routers import voice_api
from app.services import voice_reference

PK = "a" * 64
TWENTY_MB = 20 * 1024 * 1024


def _fake_ffmpeg(tmp_path):
    """An `ffmpeg` that logs its argv and writes a 2 s mono 24 kHz WAV to its last argument."""
    log = tmp_path / "ffmpeg.log"
    script = tmp_path / "ffmpeg"
    script.write_text(f"""#!{sys.executable}
import json, sys, wave
a = sys.argv[1:]
with open({str(log)!r}, "a") as f:
    f.write(json.dumps(a) + "\\n")
src = a[a.index("-i") + 1]
with open(src, "rb") as f:
    if f.read(4) == b"NOPE":
        sys.stderr.write("Invalid data found when processing input")
        sys.exit(1)
w = wave.open(a[-1], "wb")
w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
w.writeframes(b"\\0\\0" * 48000)
w.close()
""")
    script.chmod(0o755)
    return str(script), log


def _settings(key, default=None):
    return {"voice_enabled": "true", "voice_max_ref_seconds": "30", "voice_max_chars": "800"}.get(key, default)


@pytest.fixture
def app(tmp_path):
    ff, log = _fake_ffmpeg(tmp_path)
    seen = {}

    async def generate_voice(db, text, ref_bytes, reference_path=None):
        seen["text"], seen["ref"] = text, ref_bytes
        return b"RIFF-spoken", "local"

    async def generate_local(db, text, path):
        with open(path, "rb") as f:
            seen["text"], seen["ref"] = text, f.read()
        return b"RIFF-spoken"

    a = FastAPI()
    a.include_router(client_router.router)
    a.include_router(voice_api.router)
    a.dependency_overrides[get_db] = lambda: None
    a.dependency_overrides[voice_api.get_voice_auth] = lambda: True
    client_router._voice_busy.clear()
    client_router._voice_cooldown.clear()
    with mock.patch.object(client_router, "_verify_self_auth", lambda *a, **k: True), \
         mock.patch("app.services.settings_store.get", _settings), \
         mock.patch("app.services.voice_local.is_available", lambda: True), \
         mock.patch("app.services.voice_factory.generate_voice", generate_voice), \
         mock.patch("app.services.voice_factory._generate_local", generate_local), \
         mock.patch("app.services.media_service.resolve_ffmpeg", lambda: ff):
        yield TestClient(a), seen, log
    client_router._voice_busy.clear()
    client_router._voice_cooldown.clear()


def _video(n=TWENTY_MB):
    # An mp4-shaped header followed by padding: what matters to the endpoint is the SIZE.
    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * (n - 12)


def _is_normalised_wav(data):
    w = wave.open(io.BytesIO(data))
    return w.getnchannels() == 1 and w.getframerate() == 24000 and w.getnframes() / 24000 <= 30


def _ffmpeg_calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _trims_to_30s_mono_24k(argv):
    return argv[argv.index("-t") + 1] == "30" and "24000" in argv and argv[argv.index("-ac") + 1] == "1"


def test_a_20mb_phone_video_speaks_a_one_word_line(app):
    client, seen, log = app
    r = client.post("/client/voice/speak", data={"text": "hi", "pubkey": PK, "auth": "x"},
                    files={"reference": ("ref.mp4", _video(), "video/mp4")})
    assert r.status_code == 200, r.text
    assert seen["text"] == "hi"
    assert _is_normalised_wav(seen["ref"]), "the model must be handed the trimmed mono 24 kHz WAV"
    assert len(seen["ref"]) < 2 * 1024 * 1024
    assert any(_trims_to_30s_mono_24k(a) for a in _ffmpeg_calls(log)), "the 30 s trim must still run"


def test_an_over_cap_file_is_refused_by_SIZE_in_megabytes(app):
    client, seen, _ = app
    with mock.patch.object(voice_reference, "MAX_REFERENCE_BYTES", 1024 * 1024):
        r = client.post("/client/voice/speak", data={"text": "hi", "pubkey": PK, "auth": "x"},
                        files={"reference": ("ref.mp4", _video(2 * 1024 * 1024), "video/mp4")})
    assert r.status_code == 413
    detail = r.json()["detail"]
    assert "over 1 MB" in detail, detail
    assert "long" not in detail, "a refusal about BYTES must not talk about length: " + detail
    assert "ref" not in seen, "nothing reached the GPU"


def test_the_default_cap_fits_a_phone_video():
    assert voice_reference.MAX_REFERENCE_BYTES >= 200 * 1024 * 1024


def test_the_reference_endpoint_returns_the_small_wav_once(app):
    """The studio normalises a voice ONCE (when saved, or on first use of an old one) and keeps the
    small WAV, so a big video is never re-uploaded per line."""
    client, seen, log = app
    r = client.post("/client/voice/reference", data={"pubkey": PK, "auth": "x"},
                    files={"reference": ("clip.mp4", _video(), "video/mp4")})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/wav")
    assert _is_normalised_wav(r.content) and len(r.content) < 2 * 1024 * 1024
    assert float(r.headers["x-ref-seconds"]) == pytest.approx(2.0, abs=0.01)
    assert any(_trims_to_30s_mono_24k(a) for a in _ffmpeg_calls(log))
    assert "ref" not in seen, "normalising is CPU work — it never touches the voice model"


def test_the_reference_endpoint_refuses_a_bad_proof(app):
    client, _, _ = app
    with mock.patch.object(client_router, "_verify_self_auth", lambda *a, **k: False):
        r = client.post("/client/voice/reference", data={"pubkey": PK, "auth": "x"},
                        files={"reference": ("clip.mp4", b"x" * 100, "video/mp4")})
    assert r.status_code == 401


def test_the_reference_endpoint_says_when_there_is_no_audio(app):
    client, _, _ = app
    r = client.post("/client/voice/reference", data={"pubkey": PK, "auth": "x"},
                    files={"reference": ("clip.mp4", b"NOPE" + b"\0" * 100, "video/mp4")})
    assert r.status_code == 400
    assert "audio" in r.json()["detail"]


def test_the_node_api_takes_the_same_cap_and_normalises(app):
    client, seen, log = app
    r = client.post("/api/generate-voice", data={"text": "hi"},
                    files={"reference": ("ref.mp4", _video(), "video/mp4")})
    assert r.status_code == 200, r.text
    assert _is_normalised_wav(seen["ref"]), "an API caller's raw clip is normalised like every other path"
    with mock.patch.object(voice_reference, "MAX_REFERENCE_BYTES", 1024 * 1024):
        r = client.post("/api/generate-voice", data={"text": "hi"},
                        files={"reference": ("ref.mp4", _video(2 * 1024 * 1024), "video/mp4")})
    assert r.status_code == 413 and "over 1 MB" in r.json()["error"], r.text


def test_the_command_paths_share_the_cap_and_its_wording(app, tmp_path):
    """`voice`/`talk` (and the talking reply bot) normalise through normalize_reference."""
    import asyncio
    with mock.patch.object(voice_reference, "MAX_REFERENCE_BYTES", 1024 * 1024):
        path, wav, err = asyncio.run(voice_reference.normalize_reference(
            _video(2 * 1024 * 1024), "clip.mp4", str(tmp_path)))
    assert path is None and wav is None and "over 1 MB" in err and "long" not in err
    path, wav, err = asyncio.run(voice_reference.normalize_reference(_video(), "clip.mp4", str(tmp_path)))
    assert err is None and _is_normalised_wav(wav)
