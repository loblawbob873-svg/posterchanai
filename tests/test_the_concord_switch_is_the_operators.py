"""A feature checkbox must be switchable OFF, even when its prerequisite is configured.

Reported as "In bots, concord can never be unchecked, wtf is this" — and it was exactly that. An
earlier fix in this same session cured a real silent failure ("i don't see bot in room despite what
the UI says": an invite stored on a bot whose `modes` lacked `--concord`, so it held a room it never
opened) by making the invite FORCE the mode in three places:

  * `admin-bots.js` force-ticked the box and set `disabled` while an invite existed,
  * `_buildModes` re-added `--concord` on save regardless of the box,
  * `bot_manager_service._cmd_for` appended it on every spawn.

Together those made the operator's choice unexpressible. The silent failure was that it was
INVISIBLE, not that it was allowed — so the mismatch is now SAID (a warning in the form and in the
manager's log) and the tick box decides.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static/js/admin-bots.js").read_text(encoding="utf-8")
MGR = (ROOT / "app/services/bot_manager_service.py").read_text(encoding="utf-8")


class TheBoxCanBeUnticked(unittest.TestCase):

    def test_the_checkbox_is_never_disabled_by_the_invite(self):
        self.assertNotIn("box.disabled = !!inv;", JS)
        self.assertIn("box.disabled = false;", JS)

    def test_saving_records_the_box_not_the_invite(self):
        """`_buildModes` must not re-add the mode, or unticking is undone on Save."""
        body = JS[JS.index("function _buildModes("):]
        body = body[:body.index("\n}", 5)]
        self.assertNotIn("modes.add('--concord')", body)

    def test_the_manager_does_not_force_it_on_spawn(self):
        """The last place it was forced — and the one that overrode the column on every restart."""
        self.assertNotIn('modes = list(modes) + ["--concord"]', MGR)

    def test_the_mismatch_is_reported_in_both_places(self):
        """The original bug was an invisible no-op. Keeping it visible is what makes leaving the
        decision with the operator safe."""
        # A short phrase, because both messages wrap across source-string concatenation breaks and
        # a longer literal would be asserting the line wrapping rather than the message.
        self.assertIn("join that room", JS)
        self.assertIn("join that room", MGR)
        self.assertIn("listener is switched OFF", MGR)
        self.assertIn("listener is OFF", JS)

    def test_pasting_an_invite_still_offers_to_turn_it_on(self):
        """Convenience is fine; a lock is not. It ticks once, and only if the operator has not
        already expressed a view for this invite."""
        self.assertIn("pcSeenInvite", JS)
        self.assertIn("box.checked = true", JS)


if __name__ == "__main__":
    unittest.main()
