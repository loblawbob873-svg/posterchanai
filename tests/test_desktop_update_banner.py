"""The desktop finally learns a build is waiting.

Every stale-shell disaster this week — the signer wedging on each restart, "deleting is basically
broken", the invisible trash — was a desktop running old bundled code with NO way to know. The
bundle bakes the SW cache version it was built from; the client compares it against the server's
live sw.js (bumped on every client change) and rides the existing update row. No endpoint, no CI
plumbing — the number already exists on both sides."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
BUILD = open(os.path.join(ROOT, "desktop", "build-www.sh"), encoding="utf-8").read()


class DesktopUpdateBanner(unittest.TestCase):
    def test_the_bundle_bakes_the_version_it_was_built_from(self):
        self.assertIn("__PC_DESKTOP_BUILD__", BUILD)
        self.assertIn("pc-nostr-v", BUILD)

    def test_the_client_compares_against_the_live_sw(self):
        a = APP.index("async function _checkDesktopUpdate()")
        seg = APP[a:a + 1400]
        self.assertIn("window.Capacitor", seg, "the check must not fire in the APK")
        self.assertIn("pc-nostr-v", seg)
        self.assertIn("__PC_DESKTOP_BUILD__", seg)

    def test_the_tap_opens_the_download_page_not_an_apk(self):
        a = APP.index("function applyUpdate()")
        seg = APP[a:a + 1600]
        self.assertIn("_desktopUpdate", seg)
        self.assertIn("/desktop/", seg)
        self.assertLess(seg.index("_desktopUpdate"), seg.index("_fromZapstore"),
                        "the desktop branch must be checked before the APK ones")

    def test_the_check_is_wired_to_the_boot_and_hourly_triggers(self):
        self.assertIn("_checkDesktopUpdate(); }catch(_){} }, 4000)", APP.replace("_checkApkUpdate(); ", ""))
