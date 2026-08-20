"""A native window goes where its frame goes — including when the frame is maximised or restored.

    "maximizing and unmaximizing removes the border. Weird"

It is not weird and the border is not removed. In shell mode a native app is a real compositor
surface floating over a hole in OUR chrome; the two only stay together because `nsync()` tells sway
where the hole went. Every path that changes geometry has to say so.

`snapTo` and `unsnap` did not. Maximising moved our chrome to fill the screen and left the app at its
old size; restoring moved the chrome back while the app stayed FULL SCREEN, parked on top of the
frame it was supposed to sit inside. The border is drawn the whole time, with an application-sized
surface covering it.

It hid this long because a manual resize ends in a gesture, and the gesture path DOES sync — so
dragging a window's corner looked fine and only the maximise button, which is a click, was wrong.

This reads the shipped os.js rather than a copy, and checks the calls are inside the two functions
rather than merely present in the file.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OS_JS = ROOT / "static" / "js" / "client" / "os.js"


def strip_comments(src):
    """JS with comments and strings removed — the fix's own doc block names both functions and
    `nsync` several times, and matching prose is how these guards rot."""
    out, i, n, = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            q, j = c, i + 1
            while j < n and src[j] != q:
                j += 2 if src[j] == "\\" else 1
            out.append(" ")
            i = j + 1
        elif src.startswith("//", i):
            i = src.find("\n", i)
            if i < 0:
                break
        elif src.startswith("/*", i):
            i = src.find("*/", i)
            i = n if i < 0 else i + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def body(src, decl):
    """One function, brace-matched from its declaration."""
    i = src.index(decl)
    j = src.index("{", i)
    depth, k = 0, j
    while True:
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1


class GeometryChangesTellTheCompositor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = strip_comments(OS_JS.read_text())

    def test_maximising_moves_the_app_too(self):
        self.assertIn("nsync()", body(self.src, "function snapTo"),
                      "snapTo moves our chrome without telling sway where the app should go, so the "
                      "app stays where it was")

    def test_restoring_moves_the_app_back(self):
        self.assertIn("nsync()", body(self.src, "function unsnap"),
                      "unsnap restores the frame and leaves a full-screen app sitting on top of it — "
                      "which is what 'the border disappeared' actually is")

    def test_neither_pays_for_it_on_a_desktop_with_no_native_windows(self):
        """`nsync` walks every window and talks to sway over IPC. The other call sites guard on
        there being a native window at all; a desktop of only PosterChan windows must not start
        doing compositor round-trips on every maximise."""
        for fn in ("function snapTo", "function unsnap"):
            with self.subTest(fn=fn):
                b = body(self.src, fn)
                self.assertRegex(b, r"nativeWins\(\)\.length\s*\)\s*nsync\(\)",
                                 "%s calls nsync unguarded" % fn)

    def test_it_syncs_after_the_geometry_is_written(self):
        """Reading the frame before the style lands measures the OLD rectangle, which sends the app
        to where it already was and leaves the symptom exactly as it is."""
        for fn, anchor in (("function snapTo", "Object.assign(w.el.style, css)"),
                           ("function unsnap", "Object.assign(w.el.style,")):
            with self.subTest(fn=fn):
                b = body(self.src, fn)
                self.assertLess(b.index(anchor), b.index("nsync()"),
                                "%s syncs before writing the new geometry" % fn)


class TheOtherGeometryPathsStillSync(unittest.TestCase):
    """The rule this bug broke, pinned so the next edit does not lose one of the others."""

    def test_every_known_mover_tells_the_compositor(self):
        src = strip_comments(OS_JS.read_text())
        for fn in ("function snapTo", "function unsnap", "function minimise"):
            with self.subTest(fn=fn):
                self.assertIn("nsync()", body(src, fn))


if __name__ == "__main__":
    unittest.main()
