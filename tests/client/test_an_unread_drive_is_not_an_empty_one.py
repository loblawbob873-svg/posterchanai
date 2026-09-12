"""The wallpaper picker must not report "no pictures" about a drive it never read.

`backgrounds()` reads the files index SYNCHRONOUSLY, and `wallpaperPicker()` never called
`ensure()`. So whenever the index had not been materialised yet, the panel rendered its empty state:
"No pictures yet — make a folder called Backgrounds in Files → Blossom and put some images in it" —
an instruction to do the exact thing the user had already done, printed over a folder that already
had pictures in it.

Measured on a real desktop: the index held 25 folders and 6,945 files and loaded in 708ms once
something asked for it, while the picker had been reporting no pictures for two days. Nothing had
asked, and the obvious way to make it ask (opening Files) was blocked on the same read.

A picker has three states, never two: reading, a real answer, and "could not read your drive".
"""
import re
import unittest
from pathlib import Path

OS_JS = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "os.js"


def _code_only(src):
    """Blank out /* … */ and // … comments, preserving offsets so ordering tests stay meaningful."""
    out, i, n = [], 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "/*":
            j = src.find("*/", i + 2); j = n if j < 0 else j + 2
            out.append(" " * (j - i)); i = j
        elif two == "//":
            j = src.find("\n", i); j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        else:
            out.append(src[i]); i += 1
    return "".join(out)


class UnreadDrive(unittest.TestCase):
    def setUp(self):
        # STRIP COMMENTS. The previous version searched the raw text, and the FIRST occurrence of
        # "ensure" inside _fillWallpaperPicker was the comment above the call — so deleting the
        # `await idx.ensure()` and keeping the prose left the test passing. That is exactly the
        # vacuous proof this repo keeps rediscovering, committed in a test written to prevent one.
        self.src = _code_only(OS_JS.read_text())
        m = re.search(r"async function _fillWallpaperPicker\(m\)\{.*?\n  \}", self.src, re.S)
        self.assertIsNotNone(m, "_fillWallpaperPicker not found — the fix is gone")
        self.fill = m.group(0)

    def test_the_index_is_loaded_before_the_answer_is_given(self):
        """The regression: the empty state was reachable without ever asking the drive."""
        self.assertIn("ensure", self.fill,
                      "the picker still decides 'no pictures' without loading the index")
        ensure_at = self.fill.index("ensure")
        empty_at = self.fill.index("No pictures yet")
        self.assertLess(ensure_at, empty_at,
                        "the empty state is decided before the index is read")

    def test_a_failed_read_is_not_reported_as_an_empty_folder(self):
        """'Could not ask' is never 'you have none' — the rule this repo keeps re-learning."""
        self.assertIn("Couldn’t read your drive", self.fill,
                      "a failed index read has no distinct state")
        self.assertIn("not an empty folder", self.fill,
                      "the failure message does not distinguish itself from an empty folder")
        self.assertIn("os-bg-retry", self.fill, "a failed read offers no retry")

    def test_the_empty_state_survives_for_the_case_it_is_actually_true(self):
        """A drive that really has no Backgrounds still gets the instructions."""
        self.assertIn("No pictures yet", self.fill)
        self.assertIn("os-bg-files", self.fill, "the 'Open Files' way out was lost")

    def test_the_read_is_bounded(self):
        """ensure() -> pull() -> _pull() asks a REMOTE SIGNER first, which may be a phone that is
        asleep, and the index fetch after it is root-relative so the client's media ceiling does not
        cover it. Unraced, the panel sits on "Reading your drive…" for ever with ✕ as the only exit
        — a worse third state than the wrong answer it replaced."""
        self.assertIn("Promise.race", self.fill,
                      "the wallpaper picker awaits the index with no ceiling; it can hang for ever")
        self.assertRegex(self.fill, r"setTimeout\(.*?,\s*\d{3,}\s*\)",
                         "no timeout value in the race")

    def test_the_picker_paints_before_it_waits(self):
        """A panel that appears only after a network read is indistinguishable from a dead button."""
        opener = re.search(r"async function wallpaperPicker\(\)\{.*?\n  \}", self.src, re.S)
        self.assertIsNotNone(opener, "wallpaperPicker not found")
        body = opener.group(0)
        self.assertIn("Reading your drive", body,
                      "the panel does not show a reading state before awaiting the index")
        self.assertLess(body.index("root.appendChild(m)"), body.index("_fillWallpaperPicker"),
                        "the panel is only attached after the read")


if __name__ == "__main__":
    unittest.main()
