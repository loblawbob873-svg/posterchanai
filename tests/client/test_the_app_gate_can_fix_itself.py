"""The instance gate must FIX the problem, not describe it in every app.

Reported as: "The nip05 message you get when running every fucking app is annoying, and terrible to
the user, surely you can improve this. There has to be a smarter something you can do when you login
to quickly get where you need to be."

The gate appears whenever a published profile does not carry an address this node granted. It used
to say, in every app, every time: "Open Edit profile, replace the NIP-05 / verified address field
with this address, then Save." But `/api/instance-welcome/access` returns the address the instance
ALREADY GRANTED — so the app was displaying the exact string it wanted, and asking the person to
retype it somewhere else. That is work the app should do.

The rule: when the instance knows the address, the gate offers ONE button that publishes it and
continues to the app that was asked for.
"""
import re
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "static/js/client/instance-access.js").read_text()
GATE = SRC[SRC.index("function gate(view,host)"):]


class TheGateOffersTheFix(unittest.TestCase):
    def test_it_offers_a_one_click_button_when_the_address_is_known(self):
        self.assertIn("ia-use", GATE, "the gate no longer offers to set the address for you")
        self.assertIn("Use '+esc(address)", GATE,
                      "the button does not name the address, so it reads as a generic action")

    def test_the_button_only_appears_when_there_is_an_address_to_set(self):
        """With no granted name there is nothing to publish — applying is the only honest next step."""
        self.assertIn("(address?'<button class=\"btn btn-neon ia-use\">", GATE)

    def test_it_stops_telling_people_to_retype_it(self):
        self.assertNotIn("replace the <strong>NIP-05 / verified address</strong> field", GATE,
                         "the gate still asks the user to do by hand what the button now does")


class ItPublishesSafely(unittest.TestCase):
    def test_the_existing_profile_is_read_and_one_field_changed(self):
        """A kind-0 is REPLACEABLE and carries the whole document. Publishing a fresh object would
        drop the name, picture, lud16 and every payment alias — the replaceable-document wipe."""
        self.assertIn("{...cur, nip05:address}", GATE,
                      "the gate publishes a profile built from scratch, which erases every other field")
        self.assertIn("p.profOf&&p.profOf(pk)", GATE, "it reads the profile through a name the surface does not export")
        self.assertIn("throw Error('Could not read your current profile", GATE,
                      "with no readable profile it publishes anyway — that is the wipe")

    def test_it_uses_only_functions_the_shared_surface_really_exports(self):
        """`PC.openMenuPopover` was missing from `window.__PC` and looked present — a call that
        throws exactly where an error is being reported. Check the ones this file now needs."""
        app = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text()
        surface = app[app.index("window.__PC = {"):]
        surface = surface[:surface.index("};")]
        for name in ("publish", "profOf", "viewer", "retryInstanceView", "toast"):
            self.assertRegex(surface, r"(^|[,{\s])" + name + r"\s*[,:}]",
                             f"instance-access.js calls PC.{name}, which is not exported")

    def test_success_continues_to_the_app_that_was_asked_for(self):
        self.assertIn("p.retryInstanceView(view)", GATE,
                      "setting the address leaves the person staring at the gate they just cleared")

    def test_a_slow_relay_does_not_look_like_a_failure(self):
        """A published kind-0 is not instantly queryable; saying 'failed' there would send somebody
        round the loop again for something that worked."""
        self.assertIn("Relays can take a moment", GATE)


if __name__ == "__main__":
    unittest.main()
