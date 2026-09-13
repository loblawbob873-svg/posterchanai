"""Every relay headcount must say what it is a count OF.

"72 people are now on the relay? looks like you fucked up something else there too" — and 72 was the
CORRECT number. router.lan's nginx trusted only 192.168.0.1 in `set_real_ip_from` while the
cloudflared tunnel dials the site's public address, so the entire internet arrived as one IP and was
counted once; the 2026-09-04 fix turned that into real per-client addresses. The user saw the
corrected figure 6m34s after a relay restart, mid-reconnect.

A number nobody can reconcile is worth as little as a wrong one. So each surface that draws a
headcount carries what it was deduped from — the distinct addresses AND the open connections they
came from — and the "people active" tile says which keys it excludes, because NIP-17 signs every DM
with a throwaway key and counting those reported 19,347 "people" over 30 days on a node with 128
granted names.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "static" / "js" / "client"


def _code_only(src):
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


class HeadcountsAreLabelled(unittest.TestCase):
    def setUp(self):
        self.app = _code_only((ROOT / "app.js").read_text())
        self.stats = _code_only((ROOT / "stats.js").read_text())
        self.os = _code_only((ROOT / "os.js").read_text())

    def test_the_sidebar_chip_reports_the_sockets_it_deduped(self):
        """The regression: the chip said only "People connected to this relay right now"."""
        self.assertIn("relay_sockets", self.app,
                      "the sidebar never reads the socket count it was deduped from")
        self.assertIn("distinct client addresses", self.app,
                      "the chip does not say what its number counts")

    def test_the_chip_degrades_when_the_relay_cannot_say(self):
        """An older relay build ships no breakdown; the chip must not invent one."""
        self.assertIn("People connected to this relay right now", self.app,
                      "the fallback wording is gone, so an older relay renders a blank title")

    def test_the_phone_sheet_says_the_same_thing(self):
        """The ☰ More sheet is built synchronously from a cached value."""
        self.assertIn("_lastRelayTitle", self.app,
                      "the phone sheet still shows the old unlabelled title")

    def test_the_desktop_cells_have_tips_of_their_own(self):
        """Both desktop renderers used the LABEL as the title, i.e. said nothing."""
        self.assertRegex(self.os, r"tip:\s*relayTip",
                         "the desktop 'on relay' cell has no tooltip of its own")
        self.assertNotRegex(self.os, r'title="\$\{enc\(c\.label\)\}"',
                            "a stat cell's tooltip still just repeats its own label")
        self.assertIn("c.tip || c.label", self.os,
                      "the widget/tray renderers do not use the cell's tip")

    def test_people_active_says_which_keys_it_excludes(self):
        self.assertIn("throwaway keys", self.stats,
                      "the 'people active' tile does not say it excludes one-time DM keys")

    def test_people_connected_mentions_our_own_machines(self):
        self.assertIn("online_internal", self.stats,
                      "the 'people connected' tile does not report this node's own machines")


if __name__ == "__main__":
    unittest.main()
