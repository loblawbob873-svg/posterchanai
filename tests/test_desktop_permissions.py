"""What the Electron shell grants the client (desktop/main.js).

Run: venv-unified/bin/python -m unittest tests.test_desktop_permissions

The desktop app is a thin shell around the SAME /client that runs in a browser, so anything the
shell forgets to grant becomes a feature that works everywhere except on the desktop — and the
report comes back as "it's broken on Windows", which sends you looking in the wrong file.

That is exactly what happened to the Notes BACKUP. `showSaveFilePicker` is the only way to write a
multi-gigabyte archive without holding it in memory, Electron gates it behind the `fileSystem`
permission, and the shell's allowlist did not have it — so the picker was denied, the client fell
back to the in-memory path, and a library with attachments could not be saved at all.

This pins the allowlist itself. It is deliberately a list, not a "contains fileSystem" check: the
point is that adding or removing a grant is a decision someone has to make on purpose, and every
entry here is one the client actually uses.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "desktop", "main.js")

# Each of these is used by the client, and named where it is used.
EXPECTED = {
    "media",                        # calls: getUserMedia
    "notifications",                # web push / OS notifications
    "fullscreen",                   # video + streams
    "clipboard-read",               # paste into the composer
    "clipboard-sanitized-write",    # copy buttons (npub, invoice, password)
    "display-capture",              # screen share while live
    "pointerLock",                  # games
    "background-sync",              # the offline outbox
    "fileSystem",                   # showSaveFilePicker: the Notes backup, and any save-to-disk
}


def _allow_set():
    with open(MAIN, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"const ALLOW = new Set\(\[(.*?)\]\)", src, re.S)
    assert m, "could not find the ALLOW set in desktop/main.js"
    return set(re.findall(r"'([^']+)'", m.group(1)))


class DesktopPermissions(unittest.TestCase):
    def test_the_allowlist_is_exactly_what_the_client_needs(self):
        self.assertEqual(_allow_set(), EXPECTED)

    def test_the_file_picker_is_granted(self):
        """Named on its own because it is the one that was missing, and because the failure it
        produces (a backup that cannot be saved) points at the wrong half of the codebase."""
        self.assertIn("fileSystem", _allow_set(),
                      "Electron denies showSaveFilePicker without this, so the Notes backup — and "
                      "every other save-to-disk — fails on the desktop app only")

    def test_grants_are_still_scoped_to_our_own_origin(self):
        """A permission allowlist is only half of it: without isOurs(), any page the shell ever
        loads would inherit the lot."""
        with open(MAIN, encoding="utf-8") as fh:
            src = fh.read()
        req = src.index("setPermissionRequestHandler")
        chk = src.index("setPermissionCheckHandler")
        self.assertIn("permissionAllowed(permission, from)", src[req:req + 900])
        self.assertIn("permissionAllowed(permission, from)", src[chk:chk + 500])
        gate = src[src.index("const permissionAllowed"):req]
        self.assertIn("isOurs(from)", gate)

    def test_webxdc_gets_only_game_permissions(self):
        with open(MAIN, encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"const WEBXDC_ALLOW = new Set\(\[(.*?)\]\)", src, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(set(re.findall(r"'([^']+)'", m.group(1))), {"pointerLock", "fullscreen"})
        self.assertIn("WEBXDC_ALLOW.has(permission) && isWebxdcSandbox(from)", src)

    def test_downloads_are_still_wired(self):
        """The in-memory fallback saves through an <a download>, which in Electron only reaches a
        file if something handles will-download."""
        with open(MAIN, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("will-download", src)


if __name__ == "__main__":
    unittest.main()
