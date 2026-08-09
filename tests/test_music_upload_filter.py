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


def _extract_has_src():
    src = APP.read_text()
    m = re.search(r"function _musicHasSrc\(file\)\{.*?\n  \}", src, re.S)
    assert m, "_musicHasSrc not found in app.js"
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_deleted_track_is_not_treated_as_already_imported():
    """The deadlock: index entry survives, blob does not, and the library can never be restored.

    Deleting your files removes the bytes from Blossom but leaves the entry in the files index.
    musicTracks() hides any track the server no longer lists, so the library reads "no music yet" —
    while _musicHasSrc, which only looked at the index, refused every re-upload as "already
    imported". Someone who cleared their drive could never put their music back.

    The null case matters as much as the missing one: _blobHave is null until the server list has
    been fetched, and treating unknown as "gone" would make every dedup check fail open and
    re-upload the whole library.
    """
    fn = _extract_has_src()
    harness = """
      const FilesIdx = { _norm: () => ({ files: {
        aaa: { folder:'Music', srcName:'song.mp3', srcSize:100 },
        bbb: { folder:'Music', srcName:'other.mp3', srcSize:200 },
        ccc: { folder:'Docs',  srcName:'song.mp3', srcSize:100 },
      }}) };
      let _blobHave = null;
      %s
      const f = { name:'song.mp3', size:100 };
      const out = {};
      _blobHave = new Set(['aaa','bbb']);      out.blobPresent = _musicHasSrc(f);
      _blobHave = new Set(['bbb']);            out.blobDeleted = _musicHasSrc(f);
      _blobHave = null;                        out.notFetchedYet = _musicHasSrc(f);
      _blobHave = new Set(['aaa']);            out.wrongFolderIgnored = _musicHasSrc({name:'song.mp3', size:999});
      console.log(JSON.stringify(out));
    """ % fn
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip())
    assert got["blobPresent"] is True, "a track whose blob is still on the server IS already imported"
    assert got["blobDeleted"] is False, (
        "a track whose blob was DELETED must not count as already imported — that is the deadlock "
        "that makes a cleared library impossible to restore")
    assert got["notFetchedYet"] is True, (
        "_blobHave null means 'not fetched yet', not 'everything is gone' — treating it as missing "
        "would re-upload the entire library")
    assert got["wrongFolderIgnored"] is False


def test_transport_actions_never_land_on_pause():
    """⏮ ⏭ and Shuffle all must not silently PAUSE.

    play(sha) treats "the track you asked for is the one already playing" as pause/resume, which is
    right for tapping a row in the list and wrong for every transport button — with a short queue
    they all resolve to the current track (next wraps (i+1)%1 back to itself, prev likewise, and a
    random pick can choose it), so all three paused instead of playing. `force` distinguishes them.

    A structural check: the behaviour lives inside an async method wired to a real <audio>, so this
    asserts the flag is still threaded through every transport path rather than re-simulating one.
    """
    src = APP.read_text()
    guard = re.search(r"if\(!\(opts&&opts\.force\) && sha===this\.cur", src)
    assert guard, "play() no longer distinguishes a transport action from tapping the playing track"
    # …to the end of each method, not the end of its first line — both span several lines now.
    nxt = re.search(r"    next\(\)\{.*?\},\n", src, re.S)
    prv = re.search(r"    prev\(\)\{.*?\},\n", src, re.S)
    assert nxt and "force:true" in nxt.group(0), "next() must force — it can resolve to the current track"
    assert prv and "force:true" in prv.group(0), "prev() must force — it can resolve to the current track"
    # EVERY shuffle entry point, not just the first — the classic Music button and the app's
    # "Shuffle all" are two of them, and only one had been fixed.
    picks = re.findall(r"MusicPlayer\.play\(MusicPlayer\.queue\[Math\.floor[^\n]*", src)
    assert picks, "no shuffle pick found — did the shuffle move?"
    assert all("force:true" in p for p in picks), (
        "a shuffle pick can be the track already playing; without force that pauses: "
        + "; ".join(p[:90] for p in picks if "force:true" not in p))
    assert src.count("id=\"ma-shuf\"") == 0, (
        "the app header's shuffle was removed in favour of the list header's Shuffle all — two "
        "shuffles that set the same flag is one too many")
