"""The launcher and the app MUST share one process, and the reason is a static field.

Asked for as "all the launcher apps should be separate processes if possible to avoid fucking each
other up". The instinct is right — a home screen that dies with the app it launches is a phone with
no home screen — but `android:process=":home"` is not the way to get it here, and the change would
be silent.

WHAT WOULD BREAK. `ACTION_MAIN` + `CATEGORY_LAUNCHER` delivered to a `singleTask` activity DISCARDS
its extras, so a launcher tile could not say which screen it meant and every tile opened whatever
was last on screen. The fix parks the request in `LaunchView` — a class of PRIVATE STATIC FIELDS
written on the launcher side and read by MainActivity. Statics are per-process. Move HomeActivity to
its own process and MainActivity reads a LaunchView that was never written: no crash, no log, every
tile opening the last screen again. That bug has already been found and fixed once.

WHAT IS ALREADY ISOLATED, which is the part the request is actually about. The WebView's RENDERER is
a separate process by Chromium's own design, and it is the half that dies under memory pressure —
`MainActivity.surviveRenderProcessDeath` exists precisely because Android kills it. So the crash
most likely to "take everything down" already cannot: it is caught and the page rebuilt. Splitting
the Java activities instead would duplicate the Capacitor/WebView stack per process on a device
whose problem is memory pressure in the first place.

So this is a guard, not a preference: if the launcher is ever moved to its own process, LaunchView
has to stop being a static first (a ContentProvider, a file, or the extras a non-singleTask
activity keeps). This test fails the moment the manifest grows an `android:process` without that,
and its message says what to do.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "AndroidManifest.xml")
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java", "place", "poster", "app")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class LauncherSharesTheAppsProcess(unittest.TestCase):
    def test_no_component_declares_its_own_process(self):
        xml = read(MANIFEST)
        found = re.findall(r'android:process="([^"]+)"', xml)
        self.assertEqual([], found,
                         "a component now runs in its own process (%s). LaunchView's handoff is a "
                         "STATIC field and does not cross a process boundary: MainActivity will "
                         "read an empty one and every launcher tile will open the last screen "
                         "again, silently. Replace the static with something cross-process before "
                         "splitting." % ", ".join(found))

    def test_the_handoff_that_forbids_it_is_still_a_static(self):
        """If LaunchView stops being static, the guard above has lost its reason and should go."""
        src = read(os.path.join(JAVA, "home", "LaunchView.java"))
        self.assertRegex(src, r"private\s+static\s+String\s+pending",
                         "LaunchView no longer parks in a static — re-derive whether the launcher "
                         "may now run in its own process instead of leaving a stale rule here")
        main = read(os.path.join(JAVA, "MainActivity.java"))
        self.assertIn("LaunchView", main,
                      "MainActivity no longer reads LaunchView; the coupling this guard protects "
                      "may have moved")

    def test_the_renderer_crash_is_already_handled(self):
        """The failure the request is really about, and it is already isolated by Chromium."""
        main = read(os.path.join(JAVA, "MainActivity.java"))
        self.assertIn("onRenderProcessGone", main,
                      "nothing handles renderer death any more — that, not the Java activities, is "
                      "the process Android actually kills")


if __name__ == "__main__":
    unittest.main()
