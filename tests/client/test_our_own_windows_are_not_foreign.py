"""A FOREIGN WINDOW IS SOMEBODY ELSE'S APPLICATION, NOT ANY WINDOW THAT IS NOT THIS SURFACE.

The desktop shell is an opaque full-output surface, kept BELOW applications so it cannot cover OBS
or a popped-out window. The exception is a PosterChan window drawn INSIDE it -- System Settings,
Task Manager, Virtual Machines, folders -- and while one of those is focused the desktop genuinely
has to be in front. The renderer decides that and publishes it through `pc:wm:shell-front`.

Its test for "somebody else holds the keyboard" was

    list.find(x => x.focused && Number(x.id) !== shellId)

which counts OUR OWN popped-out windows and the other monitor's shell surface. So with a popped-out
window focused, the renderer reported a foreign application, the desktop was sunk, and an in-page
frame could never come forward. Reported as "running Global then clicking on System Settings causes
the windows to conflict, System settings never gets focus" -- Global is popped out, System Settings
is drawn inside the shell.

Somebody else's window must still sink the desktop; that is the rule this exists for, and dropping
it would bring back "opening a new window hides all the other windows".
"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def run(rows):
    """The shipped predicate, lifted and run against a window list."""
    src = OS_JS[OS_JS.index("      const OURS ="):]
    src = src[: src.index("const foreign = !!focused;") + len("const foreign = !!focused;")]
    tmp = Path(tempfile.mkdtemp())
    try:
        js = tmp / "t.js"
        js.write_text("const shellId = 5;\nconst list = " + json.dumps(rows) + ";\n"
                      + re.sub(r"^\s+", "", src, flags=re.M)
                      + "\nconsole.log(JSON.stringify({foreign}));\n")
        out = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])["foreign"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class TestOurOwnWindowsAreNotForeign(unittest.TestCase):
    def test_a_popped_out_posterchan_window(self):
        """The exact case reported: Global is focused, System Settings must still be reachable."""
        self.assertFalse(run([
            {"id": 5, "app": "place.poster.desktop", "title": "PosterChan · Nostr", "focused": False},
            {"id": 50, "app": "place.poster.desktop", "title": "PosterChan Window — global",
             "focused": True},
        ]))

    def test_the_other_monitors_shell_surface(self):
        self.assertFalse(run([
            {"id": 5, "app": "place.poster.desktop", "title": "PosterChan · Nostr", "focused": False},
            {"id": 7, "app": "place.poster.desktop", "title": "PosterChan · Nostr", "focused": True},
        ]))

    def test_the_older_app_ids_too(self):
        for app in ("posterchan", "posterchan-desktop", "PosterChan"):
            self.assertFalse(run([{"id": 9, "app": app, "title": "x", "focused": True}]), app)


class TestSomebodyElsesWindowStillSinksTheDesktop(unittest.TestCase):
    """Dropping this brings back "opening a new window hides all the other windows"."""

    def test_obs(self):
        self.assertTrue(run([{"id": 74, "app": "com.obsproject.Studio", "title": "OBS",
                              "focused": True}]))

    def test_firefox(self):
        self.assertTrue(run([{"id": 80, "app": "org.mozilla.firefox", "title": "web",
                              "focused": True}]))

    def test_a_window_with_no_app_id_is_treated_as_somebody_elses(self):
        """An unnamed surface is not evidence that it is ours, and guessing wrong here puts the
        desktop over an application."""
        self.assertTrue(run([{"id": 90, "app": "", "title": "?", "focused": True}]))

    def test_nothing_focused_is_not_foreign(self):
        self.assertFalse(run([{"id": 74, "app": "com.obsproject.Studio", "title": "OBS",
                               "focused": False}]))


if __name__ == "__main__":
    unittest.main()
