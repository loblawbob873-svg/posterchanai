"""The Music folder's "is this audio?" test, run as the SHIPPED code.

A song uploaded to Files → 🎵 Music was silently dropped with "skipped (not audio)" and the library
then said "no music yet" — which reads as the feature being broken rather than the file being
rejected. The check was `file.type.startsWith('audio/')`, and a browser's MIME guess comes from the
OS registry: it is routinely EMPTY, generic, or plain wrong for exactly the formats people keep
music in.

The cases below are not invented. They are what browsers actually report:
  - '' for .opus, .flac and .wma on a Linux desktop with no registration for them
  - 'application/octet-stream' for anything dragged out of a file manager that registers no type
  - 'video/mp4' for .m4a, because the container is MP4

`_looksAudio` is extracted from app.js and run under node, so this tests the code that ships rather
than a copy of it that can drift.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "static" / "js" / "client" / "app.js"

# (filename, browser-reported type, must be accepted)
CASES = [
    ("song.mp3", "audio/mpeg", True),
    ("song.ogg", "audio/ogg", True),
    ("song.wav", "audio/wav", True),
    # The ones the old check dropped.
    ("song.opus", "", True),
    ("song.flac", "", True),
    ("song.wma", "", True),
    ("track.m4a", "video/mp4", True),
    ("track.m4a", "", True),
    ("album.flac", "application/octet-stream", True),
    ("live.aiff", "application/octet-stream", True),
    ("mix.mka", "video/x-matroska", True),
    # …and the ones it must still drop.
    ("clip.mp4", "video/mp4", False),
    ("clip.mp4", "", False),
    ("photo.jpg", "image/jpeg", False),
    ("notes.txt", "text/plain", False),
    ("archive.zip", "application/octet-stream", False),
    ("nameless", "", False),
]


def _extract():
    src = APP.read_text()
    m_re = re.search(r"const _AUDIO_EXT = /.*?/i;", src, re.S)
    m_fn = re.search(r"function _looksAudio\(f\)\{.*?\n  \}", src, re.S)
    assert m_re, "_AUDIO_EXT not found in app.js — did the Music upload filter move?"
    assert m_fn, "_looksAudio not found in app.js — did the Music upload filter move?"
    return m_re.group(0) + "\n" + m_fn.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_looks_audio_matches_what_browsers_really_report():
    script = (
        _extract()
        + "\nconst cases = " + json.dumps([[n, t] for n, t, _ in CASES]) + ";\n"
        + "console.log(JSON.stringify(cases.map(([name, type]) => _looksAudio({name, type}))));\n"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip())
    bad = [(n, t, want, g) for (n, t, want), g in zip(CASES, got) if want != g]
    assert not bad, "wrong verdict for: " + "; ".join(
        f"{n!r} type={t!r} want={want} got={g}" for n, t, want, g in bad)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_plain_mime_check_would_still_fail_these():
    """The regression this guards, stated as a fact rather than a comment.

    Every case here is a real song that `type.startsWith('audio/')` rejects, so if someone
    "simplifies" _looksAudio back to that one line, the test above fails for a reason that is
    written down here.
    """
    dropped = [(n, t) for n, t, want in CASES if want and not t.startswith("audio/")]
    assert len(dropped) >= 6, "the interesting cases have gone missing from CASES"
