"""__blobFallback must not throw away the box the element it replaces was reserving.

Run: venv-unified/bin/python -m unittest tests.client.test_blob_fallback_dims

The report: "someone sent me a video in a DM, when I click play it shrinks to a tiny tiny square" —
on the Android APK and in a desktop browser alike.

An extensionless Blossom URL (`/<sha256>`) carries no type, so linkify renders it as an <img> with the
usual --arn/--nw size hints. The bytes are a video, the <img> fails, and __blobFallback swaps in a
<video> built by hand — copying `className` and nothing else. The hints went with the <img>, so the
CSS `width` became invalid and fell back to `auto`, which on a replaced element means "the media's own
pixels, never scaled up". Nothing looked wrong until PLAYBACK supplied the dimensions.

Measured in the real client, on a real DM, with a 128x128 clip:

    before play   video 270.8px wide   bubble 297px      (the <img>'s reserved box)
    after play    video 128x128        bubble 154x174    (the clip's literal pixels)

A bubble is width:fit-content, which is why the whole message collapsed with it — but the bug is not
the DM's. Every surface that renders an extensionless blob goes through this path; a DM is only where
the container shrink-wraps the damage.

Asserted at the SOURCE rather than in a browser because the trigger needs a URL whose bytes disagree
with its extension AND a decode failure AND playback — three things a static harness cannot stage,
while the defect itself is one missing call. tests/client/test_media_reserve.py owns the geometry.
"""
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js"


def _block(src, start):
    """The balanced {...} run beginning at the first '{' at/after `start`."""
    opener = src.index("{", start)
    depth = 0
    for i in range(opener, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[opener:i + 1]
    raise AssertionError("unbalanced block")


class TestBlobFallbackCarriesDims(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()
        cls.body = _block(cls.src, cls.src.index("window.__blobFallback"))

    def test_every_replacement_video_carries_the_hints(self):
        """Each <video> __blobFallback builds must get _carryDims before it replaces anything.

        Both branches matter and they fail identically: the authenticated blob-URL swap (the APK's
        cross-origin path) and the plain-src swap (everything else).
        """
        made = [m.start() for m in re.finditer(r"createElement\(\s*['\"]video['\"]\s*\)", self.body)]
        self.assertGreaterEqual(len(made), 2, "expected both video-swap branches in __blobFallback")
        for i, at in enumerate(made):
            # The statement run from this createElement up to the replaceWith that installs it.
            end = self.body.index("replaceWith", at)
            self.assertIn("_carryDims", self.body[at:end],
                          f"__blobFallback video branch {i + 1} builds a <video> and replaces an element "
                          f"without _carryDims — its box falls back to width:auto, i.e. the clip's own "
                          f"pixels, the moment playback supplies them")

    def test_carry_dims_moves_everything_the_box_is_built_from(self):
        """--arn/--nw are what the width is computed from, and data-dim is what lets _dimLearn fix a
        guess once the real shape arrives. Dropping any one of them silently restores the collapse."""
        fn = _block(self.src, self.src.index("function _carryDims"))
        for token in ("--arn", "--nw", "width", "height", "dim"):
            self.assertIn(token, fn, f"_carryDims no longer carries {token}")


if __name__ == "__main__":
    unittest.main()
