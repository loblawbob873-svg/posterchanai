"""The 30 s trim against REAL ffmpeg: a >20 MB clip comes out as <=30 s of mono 24 kHz WAV.

Companion to tests/test_voice_reference_size.py (which is on the gate's required list and so runs with a
stub ffmpeg). Skipped where ffmpeg is missing, which is why it is NOT on the required list.
"""
import asyncio
import io
import shutil
import subprocess
import wave
from unittest import mock

import pytest

from app.services import voice_reference

TWENTY_MB = 20 * 1024 * 1024


def _settings(key, default=None):
    return {"voice_max_ref_seconds": "30"}.get(key, default)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_real_ffmpeg_trims_a_20mb_clip_to_30_seconds(tmp_path):
    """The real thing: 60 s of 48 kHz stereo 32-bit PCM is ~23 MB; it comes out ≤30 s mono 24 kHz."""
    src = tmp_path / "big.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=60", "-ac", "2",
                    "-ar", "48000", "-c:a", "pcm_s32le", str(src)], check=True, timeout=120)
    data = src.read_bytes()
    assert len(data) > TWENTY_MB
    with mock.patch("app.services.settings_store.get", _settings):
        path, wav, err = asyncio.run(voice_reference.normalize_reference(data, "big.wav", str(tmp_path / "o")))
    assert err is None
    w = wave.open(io.BytesIO(wav))
    assert (w.getnchannels(), w.getframerate()) == (1, 24000)
    assert 29.9 <= w.getnframes() / 24000 <= 30.05
