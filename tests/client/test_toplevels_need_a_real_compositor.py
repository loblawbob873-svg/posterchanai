"""THE BRIDGE EXISTS EVERYWHERE; THE COMPOSITOR DOES NOT.

`pcWM` is injected by the preload on every platform. The comment beside that injection promises
"absent rather than broken off a compositor" -- but what is absent is the ANSWER (`available()`
says no), not the object. `PCOSWin.enabled()` tested for the OBJECT:

    if(!root.pcWM) return false;

which is true in the plain desktop app on Windows and macOS. So every PosterChan app opened as a
real compositor toplevel on a machine with no compositor to place, raise or close it. Its title
bar's own buttons call `pcWM.self()`, which has nothing to identify the window against and rejected
-- reported as "you can't maximize the Files Manager! pc:wm:self error", "all the posterchan apps
are broken with that err on windows app", and a click that opened the window AND left the view
behind it.

`PCOSShell.available()` is the cached answer to "did a compositor reply": null until asked, false
where there is none, true only once one has. A literal true is required, so a machine that has not
answered yet uses in-page frames -- what web and Android always use, and what this app did
everywhere before toplevels existed. A window opened on a machine that cannot manage it cannot be
closed, so waiting costs nothing and guessing costs the app.
"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
OSWIN = (ROOT / "static/js/client/oswin.js").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def enabled(have, has_bridge=True, off=False):
    """Run the shipped `enabled()` against a stubbed environment. `have` is what
    PCOSShell.available() answers: True, False, or None for 'not asked yet'."""
    src = OSWIN[OSWIN.index("  function enabled(){"):]
    src = src[: src.index("  /* Open one.")]
    tmp = Path(tempfile.mkdtemp())
    try:
        js = tmp / "t.js"
        js.write_text(
            "const root = { "
            + ("pcWM: {}, " if has_bridge else "")
            + "PCOSShell: { available: () => " + json.dumps(have) + " }, "
            + "localStorage: { getItem: () => " + ("'0'" if off else "null") + " } };\n"
            + "const isWindow = () => false;\n"
            + re.sub(r"^  ", "", src, flags=re.M)
            + "\nconsole.log(JSON.stringify(enabled()));\n")
        out = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class TestNoCompositorMeansNoToplevels(unittest.TestCase):
    def test_windows_and_macos(self):
        """The bridge is there and the compositor answered no. This is the reported case."""
        self.assertFalse(enabled(False))

    def test_before_the_answer_arrives(self):
        """null is 'not asked yet'. Opening a toplevel on a guess is unrecoverable if wrong."""
        self.assertFalse(enabled(None))

    def test_a_missing_shell_module_is_not_a_yes(self):
        src = OSWIN[OSWIN.index("  function enabled(){"):]
        src = src[: src.index("  /* Open one.")]
        self.assertIn("typeof root.PCOSShell.available === 'function'", src)


class TestPosterChanOsStillGetsThem(unittest.TestCase):
    def test_a_compositor_that_answered_yes(self):
        self.assertTrue(enabled(True))

    def test_the_off_switch_still_works(self):
        """`pc_os_toplevels = '0'` turns them off for one machine."""
        self.assertFalse(enabled(True, off=True))

    def test_no_bridge_at_all_is_still_no(self):
        self.assertFalse(enabled(True, has_bridge=False))


class TestTheTaskbarFallsBackToItsOwnPanels(unittest.TestCase):
    """`pcPopup` is injected on every platform too, and the start menu, notification centre, network
    panel and tray flyout all took the compositor-window path on the object's mere existence. On a
    machine with no compositor the window cannot be placed AND the in-page panel is skipped, because
    taking that branch is what skips it -- "taskbar, notifications, completely broken on windows
    app"."""

    OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")

    def test_there_is_one_named_test_for_it(self):
        self.assertIn("function _popupWindows()", self.OS_JS)

    def test_it_requires_a_compositor_that_answered(self):
        fn = self.OS_JS[self.OS_JS.index("function _popupWindows()"):]
        fn = fn[: fn.index("\n  }")]
        self.assertIn("PCOSShell.available() === true", fn)
        self.assertIn("window.pcPopup", fn)

    def test_no_surface_still_decides_on_the_bridge_object_alone(self):
        """Each of these opened a compositor window purely because the bridge existed."""
        for marker in ("pcPopup.toggle && force !== false",
                       "pcPopup.toggle && wantPopup",
                       "pcPopup.open)) return false"):
            line = [l for l in self.OS_JS.splitlines() if marker in l]
            self.assertTrue(line, f"the branch guarded by {marker!r} is gone; re-read this test")
            self.assertIn("_popupWindows()", line[0],
                          f"this branch still decides on the bridge object: {line[0].strip()}")


class TestTheTitleBarWorksWithoutACompositor(unittest.TestCase):
    """Even with the gate above, a window may exist on a machine whose compositor went away."""

    def test_self_answers_null_rather_than_rejecting(self):
        fn = MAIN[MAIN.index("ipcMain.handle('pc:wm:self'"):]
        fn = fn[: fn.index("ipcMain.handle('pc:win:control'")]
        self.assertIn("if(!wm().available()) return null;", fn)
        self.assertIn("catch(_){ return null; }", fn,
                      "a compositor that throws mid-call still rejects into the renderer")

    def test_there_is_a_platform_control_for_this_window(self):
        self.assertIn("ipcMain.handle('pc:win:control'", MAIN)
        self.assertIn("control: (action)", PRELOAD)

    def test_it_acts_only_on_the_sender(self):
        """A renderer must not be able to minimise somebody else's window."""
        fn = MAIN[MAIN.index("ipcMain.handle('pc:win:control'"):]
        fn = fn[: fn.index("\n});") + 4]
        self.assertIn("BrowserWindow.fromWebContents(e.sender)", fn)
        self.assertNotIn("getAllWindows", fn)

    def test_the_buttons_fall_back(self):
        body = OSWIN[OSWIN.index("const _act="):]
        body = body[: body.index("data-action=\"max\"") + 40]
        self.assertIn("pcWM.control", body, "the buttons still only know how to ask a compositor")


if __name__ == "__main__":
    unittest.main()
