"""A saved artifact keeps the name it came with — run the SHIPPED _artName under node.

Run: venv-unified/bin/python -m unittest tests.test_ai_artifact_names

WHAT BROKE. An /api/files/ artifact is stored content-addressed, so its URL ends `enc_<sha256>.mp3`.
Every action in the AI chat's file row took the filename from that URL, which meant a song fetched
with `ytdl` and pressed "Save to Blossom" landed in the Music library titled

    enc_c62e8fb4154111616f41c14b6ffb2fcf96d5247637ddde23cc1d04c453aafc85

— in a list whose whole purpose is browsing by name. Download and Save to Notes had the same shape
with a different mask (`posterchan-<timestamp>.mp3`).

The name was never lost: the server writes it into the markdown label (`!audio[<filename>](url)`),
which is also the only thing that survives a reload, since the payload fields are gone by then. So
these tests pin the two halves that make that work — the label→filename rule, and the wiring that
carries it from the button to the upload.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "static" / "js" / "client" / "app.js"


def _shipped_artName() -> str:
    """The real `_artName` (and the marker list it reads) lifted out of app.js.

    app.js is one 26k-line IIFE that cannot be required, so the function is extracted rather than
    reimplemented — a copy in this file would pass forever while the shipped one changed.
    """
    src = APP.read_text()
    mark = re.search(r"^  const _AI_LABEL_MARKER = .*$", src, re.M)
    fn = re.search(r"^  function _artName\(label, u\)\{.*?^  \}$", src, re.M | re.S)
    assert mark and fn, "app.js no longer defines _AI_LABEL_MARKER / _artName as expected"
    return mark.group(0) + "\n" + fn.group(0)


def name(label, url="/api/files/bob/12/enc_" + "a" * 64 + ".mp3"):
    src = (_shipped_artName()
           + f"\nconsole.log(JSON.stringify(_artName({json.dumps(label)}, {json.dumps(url)})));")
    out = subprocess.run(["node", "-e", src], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or '""')


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ArtNameTests(unittest.TestCase):
    def test_a_song_keeps_its_title(self):
        self.assertEqual(name("Rick Astley - Never Gonna Give You Up.mp3"),
                         "Rick Astley - Never Gonna Give You Up.mp3")

    def test_hyphens_and_spaces_survive(self):
        """'Artist - Title' is the shape of nearly every name yt-dlp returns. A sanitiser that
        stripped the separators would rename every song it saved."""
        self.assertEqual(name("Boards of Canada - Roygbiv"), "Boards of Canada - Roygbiv.mp3")

    def test_the_extension_follows_the_bytes(self):
        """The drive picks its icon from the extension and a download picks the app that opens it —
        and a title like 'Song (Official Video)' only looks like it has one."""
        self.assertEqual(name("Song (Official Video)"), "Song (Official Video).mp3")
        self.assertEqual(name("clip", "/api/files/b/1/enc_" + "b" * 64 + ".mp4"), "clip.mp4")

    def test_a_name_that_already_has_the_extension_does_not_get_a_second(self):
        self.assertEqual(name("track.mp3"), "track.mp3")

    def test_markers_are_not_names(self):
        """`song` / `video` are how the row knows whether an MP4 has an audio track — they are not
        filenames. A generated song has none to preserve, and must not become 'song.mp4'."""
        for m in ("song", "video", "Song", "image", "audio", "file", "media"):
            self.assertEqual(name(m), "", m)

    def test_nothing_becomes_a_path(self):
        for label in ("../../etc/passwd", "a/b/c.mp3", "C:\\Windows\\evil.mp3"):
            got = name(label)
            self.assertNotIn("/", got, label)
            self.assertNotIn("\\", got, label)
            self.assertFalse(got.startswith(".."), label)

    def test_it_is_bounded(self):
        self.assertLessEqual(len(name("L" * 400)), 124)

    def test_no_label_means_no_override(self):
        self.assertEqual(name(""), "")
        self.assertEqual(name(None), "")


class WiringTests(unittest.TestCase):
    """The name has to travel: label → data-name → handler → the File that is uploaded. Every hop
    was there to be forgotten, and forgetting one is silent — the file just saves under the hash."""

    SRC = APP.read_text()

    def test_the_three_saving_buttons_carry_the_name(self):
        row = re.search(r"function _aiFileActions\(u, kind, label\)\{.*?\n  \}", self.SRC, re.S)
        self.assertIsNotNone(row)
        body = row.group(0)
        self.assertIn("_artName(label, u)", body)
        for cls in ("ai-savefile", "ai-dlfile", "ai-notefile"):
            line = next(l for l in body.split("\n") if cls in l)
            self.assertIn('data-name=', line, f"{cls} does not carry the file's name")

    def test_the_handlers_pass_it_on(self):
        for cls, fn in (("ai-savefile", "saveFileToBlossom"), ("ai-dlfile", "downloadFileUrl"),
                        ("ai-notefile", "notesFromFileUrl")):
            line = next(l for l in self.SRC.split("\n") if f"closest('.{cls}')" in l)
            self.assertIn("dataset.name", line, f"{fn} is not given the name the button carries")

    def test_the_upload_uses_it(self):
        """_artifactFile is where the name becomes the File's name, and _keepBytes/uploadMusicTrack
        take the library title straight off `file.name`."""
        self.assertIn("async function _artifactFile(u, want)", self.SRC)
        self.assertIn("want || name ||", self.SRC)
        self.assertIn("_artifactFile(u, name)", self.SRC)


if __name__ == "__main__":
    unittest.main()
