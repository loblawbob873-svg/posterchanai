"""Signed out, the button under the profile card says "Log in" — not "Logout".

Run: venv-unified/bin/python -m unittest tests.test_guest_auth_button

Logging out RELOADS into a GUEST session: this client is never keyless, it simply has no identity of
its own. So after signing out the sidebar showed "Guest" above a button still offering to log you
out, and pressing it did the only thing it could — clear an empty session and reload — which reads
as a button that does nothing.

One button, because there is one slot. What it must not be is one LABEL. Verified in a real browser
(the shipped shell, booted as a guest, reports "Log in"); these assertions pin the wiring so it
cannot drift back.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text()
SHELL = (ROOT / "templates" / "client.html").read_text()


class AuthButtonTests(unittest.TestCase):
    def test_one_painter_owns_the_button(self):
        """It must not be wired at boot to `logout` and then re-labelled somewhere else — that is
        how the two halves disagree."""
        self.assertIn("function _paintAuthButton()", APP)
        self.assertNotIn("$('#btn-logout').onclick = logout;", APP)

    def test_a_guest_is_offered_a_way_in(self):
        fn = APP[APP.index("function _paintAuthButton()"):]
        fn = fn[:fn.index("\n  }")]
        self.assertIn("if(GUEST)", fn)
        self.assertIn("'Log in'", fn)
        self.assertIn("_leaveGuest()", fn)
        self.assertIn("b.onclick = logout;", fn)

    def test_it_is_painted_on_both_paths(self):
        """renderMe() returns EARLY for a guest, which is exactly the case this is about — so the
        guest branch has to paint before it returns."""
        fn = APP[APP.index("  function renderMe(){"):]
        fn = fn[:fn.index("\n  }")]
        guest_line = next(l for l in fn.split("\n") if "if(GUEST)" in l)
        self.assertIn("_paintAuthButton();", guest_line)
        self.assertGreaterEqual(fn.count("_paintAuthButton();"), 2)

    def test_the_shell_still_has_the_button(self):
        self.assertIn('id="btn-logout"', SHELL)


class MoreSheetTests(unittest.TestCase):
    """The phone carries the same row, and had the same problem."""

    def test_the_row_flips_for_a_guest(self):
        self.assertIn("(GUEST ? ['__login','user','Log in'] : ['logout','logout','Logout'])", APP)

    def test_the_handler_knows_both(self):
        i = APP.index("$$('.more-item',root).forEach")
        body = APP[i:APP.index("\n    });", i)]
        self.assertIn("if(v==='__login') _leaveGuest();", body)
        self.assertIn("if(v==='logout') logout();", body)

    def test_a_guest_asking_for_a_profile_is_asked_to_sign_in(self):
        """`renderProfileView(ME.pubkey)` with no ME is a crash, not a profile."""
        i = APP.index("$$('.more-item',root).forEach")
        body = APP[i:APP.index("\n    });", i)]
        self.assertIn("if(GUEST) _guestPrompt(); else renderProfileView(ME.pubkey)", body)


if __name__ == "__main__":
    unittest.main()
