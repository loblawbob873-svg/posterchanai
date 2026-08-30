"""Firefox and Telegram showed a generic square in the taskbar, on both themes.

The compositor reports `org.telegram.desktop` and `org.mozilla.firefox`; the scanned .desktop entry
matches on `telegram-desktop` and `firefox`. Neither is a prefix of the other with a separator, so
the two most-used apps on the machine matched nothing and every one of their windows fell through to
`icon:'grid'`.

The start menu had this same bug once and was fixed by resolving a real icon in the main process
(`uriFor`). The taskbar then threw that icon away, because it could not match the running window to
the app the icon belonged to — which is why fixing one did not fix the other.
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(ROOT, "tests", "client", "osshell_app_match_runtime.mjs")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class WindowsFindTheirApp(unittest.TestCase):
    def test_reverse_dns_ids_match_binary_names_and_nothing_else(self):
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-3000:])
