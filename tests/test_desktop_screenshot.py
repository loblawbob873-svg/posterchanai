"""SCREENSHOTS on PosterChanOS — desktop/screenshot.js, run under node against a stubbed grim.

Why there is a module here at all rather than `webContents.capturePage()`: the shell IS the desktop,
so asking Electron for a picture of its own window looks right and is wrong the moment anything
native is on screen. A Linux app in a PosterChan window is a real compositor surface held over a
HOLE in the page, so it is not in our window's pixels — capturePage() returns the desktop with a
black rectangle where Firefox was, which is the one thing people screenshot most.

Every test here is about a REFUSAL, because that is where this feature fails. A screenshot key that
does nothing is the worst version of it: you press it, nothing appears to happen, and you cannot
tell a missing package from a missing directory from a picture that saved somewhere you did not
look.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "desktop", "screenshot.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class Screenshot(unittest.TestCase):
    def run_js(self, body, env=None):
        js = """
        const S = require(%s);
        (async () => { const out = {};
        try { %s } catch(e){ out.threw = String(e.message || e); }
        process.stdout.write(JSON.stringify(out)); })();
        """ % (json.dumps(MOD), body)
        e = dict(os.environ)
        e.update(env or {})
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60, env=e)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def stub(self, tmp, name, script):
        """A fake `grim`/`slurp`/`wl-copy` on PATH, so this exercises the real spawn path."""
        p = os.path.join(tmp, name)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n" + script + "\n")
        os.chmod(p, 0o755)
        return p

    # ── the pure half ────────────────────────────────────────────────────────────────────────────

    def test_a_cancelled_selection_is_not_a_geometry(self):
        """slurp prints nothing and exits nonzero when somebody presses Escape. Read as a geometry
        that would be handed to grim as an argument."""
        out = self.run_js("""
          out.good = S.parseGeometry('100,200 640x480');
          out.empty = S.parseGeometry('');
          out.junk = S.parseGeometry('error: no output');
          out.neg = S.parseGeometry('-10,-20 100x100');
        """)
        self.assertEqual(out["good"], "100,200 640x480")
        self.assertIsNone(out["empty"])
        self.assertIsNone(out["junk"], "slurp's error text was accepted as a screen area")
        self.assertEqual(out["neg"], "-10,-20 100x100", "a monitor left of the origin is real")

    def test_the_name_sorts_chronologically(self):
        """A folder of screenshots is only ever read in date order."""
        out = self.run_js("""
          out.a = S.shotName(new Date(2026, 7, 20, 9, 5, 3));
          out.b = S.shotName(new Date(2026, 7, 20, 10, 5, 3));
          out.dir = S.shotDir();
        """)
        self.assertEqual(out["a"], "PosterChan-2026-08-20-090503.png")
        self.assertLess(out["a"], out["b"], "the names do not sort by time")
        self.assertTrue(out["dir"].endswith(os.path.join("Pictures", "Screenshots")))

    # ── the refusals ─────────────────────────────────────────────────────────────────────────────

    def test_no_grim_says_which_package(self):
        """The difference between an instruction and a shrug. `grim` is a package, not a
        guarantee — this is what a machine that has not installed it must be told."""
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_js("out.r = await S.capture({});",
                              {"PATH": tmp, "PC_GRIM": os.path.join(tmp, "nope")})
            self.assertIs(out["r"]["ok"], False)
            self.assertIn("grim", out["r"]["why"])
            self.assertIn("not installed", out["r"]["why"])

    def test_available_reports_region_separately(self):
        """grim alone takes the whole screen; picking an area needs slurp. A tray that hid the whole
        feature because one of the two was missing would lose the mode most people use."""
        with tempfile.TemporaryDirectory() as tmp:
            g = self.stub(tmp, "grim", "exit 0")
            out = self.run_js("out.r = await S.available();",
                              {"PC_GRIM": g, "PC_SLURP": os.path.join(tmp, "nope")})
            self.assertIs(out["r"]["ok"], True)
            self.assertIs(out["r"]["region"], False)
            self.assertEqual(out["r"]["why"], "", "a working screenshot tool reported a problem")

    def test_a_cancelled_region_is_reported_as_cancelled_not_failed(self):
        """Escape in slurp. A toast apologising every time a person changes their mind is noise, so
        the caller has to be able to tell the two apart."""
        with tempfile.TemporaryDirectory() as tmp:
            g = self.stub(tmp, "grim", "exit 0")
            sl = self.stub(tmp, "slurp", "exit 1")
            out = self.run_js("out.r = await S.capture({mode: 'region'});",
                              {"PC_GRIM": g, "PC_SLURP": sl})
            self.assertIs(out["r"]["ok"], False)
            self.assertIs(out["r"]["cancelled"], True)
            self.assertEqual(out["r"]["why"], "", "cancelling produced an error message")

    def test_an_empty_file_is_a_failure_even_though_grim_said_yes(self):
        """grim can exit 0 having written nothing when the compositor refuses the capture. A toast
        reading "saved to ~/Pictures/Screenshots/…" that names a file which is not there is worse
        than an error — it sends somebody looking for a picture that was never taken."""
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            g = self.stub(tmp, "grim", ': > "$1"')       # exit 0, zero bytes
            out = self.run_js("out.r = await S.capture({copy: false});",
                              {"PC_GRIM": g, "HOME": home})
            self.assertIs(out["r"]["ok"], False)
            self.assertIn("empty", out["r"]["why"])

    def test_a_good_capture_reports_the_path_and_the_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            g = self.stub(tmp, "grim", 'printf PNGDATA > "$1"')
            out = self.run_js("out.r = await S.capture({copy: false});",
                              {"PC_GRIM": g, "HOME": home})
            self.assertIs(out["r"]["ok"], True)
            self.assertEqual(out["r"]["bytes"], 7)
            self.assertTrue(os.path.exists(out["r"]["path"]), "the file it named is not there")
            self.assertIn(os.path.join("Pictures", "Screenshots"), out["r"]["path"])

    def test_it_never_spawns_wl_copy(self):
        """`wl-copy` DOES NOT EXIT. It forks a daemon that serves the clipboard offer until something
        else takes the selection, and that daemon inherits its parent's OPEN FILE DESCRIPTORS — here,
        the whole Electron shell's. Measured on the test machine, one screenshot left a
        `wl-copy -t image/png` holding 95 descriptors, 13 of them sockets, INCLUDING a listening
        socket of the shell.

        The visible symptom was a port that could never be bound again: the desktop was restarted,
        its listener was still held open by a clipboard process from a screenshot taken twenty
        minutes earlier, and everything connecting to it queued for ever against a socket nothing
        was accepting on. `ss -ltnp` naming `wl-copy` as the owner is the only reason it was found.

        The copy is Electron's `clipboard.writeImage` in main.js now — same clipboard, no
        subprocess, nothing to inherit. This asserts the subprocess does not come back, because the
        obvious "fix" for a screenshot that no longer copies is to add it again."""
        src = open(MOD, encoding="utf-8").read()
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        self.assertNotIn("wl-copy", code,
                         "screenshot.js spawns wl-copy again — it leaks this process's file "
                         "descriptors into a daemon that outlives the app")
        self.assertNotIn("WLCOPY", code)

    def test_capture_does_not_claim_to_have_copied(self):
        """The module cannot copy any more, so it must not say it did — main.js overwrites this
        after doing the real one. A `copied: true` here would be a toast telling somebody the
        picture is on their clipboard when it is not."""
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            g = self.stub(tmp, "grim", 'printf PNGDATA > "$1"')
            out = self.run_js("out.r = await S.capture({});", {"PC_GRIM": g, "HOME": home})
            self.assertIs(out["r"]["ok"], True)
            self.assertIs(out["r"]["copied"], False)

    def test_an_area_that_is_not_an_area_is_refused_before_grim_runs(self):
        """`geometry` reaches a command line. It is validated rather than quoted, because the shape
        of a screen area is a thing with an exact spelling and anything else is a mistake."""
        with tempfile.TemporaryDirectory() as tmp:
            g = self.stub(tmp, "grim", 'printf X > "$1"')
            out = self.run_js("out.r = await S.capture({mode: 'area', geometry: '; rm -rf /'});",
                              {"PC_GRIM": g})
            self.assertIs(out["r"]["ok"], False)
            self.assertIn("not a screen area", out["r"]["why"])


if __name__ == "__main__":
    unittest.main()
