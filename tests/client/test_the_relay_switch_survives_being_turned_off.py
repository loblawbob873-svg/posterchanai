"""Turning "use my own relays" OFF must not rewrite the list, and the Relays button must land on it.

Two reports, one screen:

  "i did turn the setting off and on!"  — and still three relays, with no way to clear them.
  "clicking the Relays button on the taskbar only brings you to settings, not Relays"

THE FIRST IS A WRITE THAT SHOULD NOT HAPPEN. The save path did

    ClientSettings.set('relaysEnabled', on); ClientSettings.set('relays', urls);

unconditionally. `urls` is read from the relay textarea, and that control is DISABLED while the
switch is off — it shows SEEDED FALLBACK suggestions, which the code's own comment says. So
switching off saved our suggestions over the user's list, and switching back on restored relays
they had never chosen. Off-and-on, the obvious way out of a bad relay list, was the thing that
locked it in.

Neither bug is new (the save line dates to 2026-06-23, the taskbar button to 2026-08-08).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


class TurningItOffChangesOnlyTheSwitch(unittest.TestCase):

    def test_the_list_is_written_only_when_the_switch_is_on(self):
        self.assertIn("ClientSettings.set('relaysEnabled', on);", APP)
        self.assertIn("if(on) ClientSettings.set('relays', urls);", APP)

    def test_the_unconditional_write_is_gone(self):
        """The exact line that caused it, which must not come back."""
        self.assertNotIn("ClientSettings.set('relaysEnabled', on); ClientSettings.set('relays', urls);", APP)

    def test_nothing_else_writes_the_list_while_the_switch_is_off(self):
        """Every `set('relays', …)` must be reachable only with the switch on, or be the explicit
        repair that clears it. A write from a disabled control is what this test exists to stop."""
        for m in re.finditer(r"ClientSettings\.set\('relays',\s*([^)]*)\)", APP):
            line_start = APP.rfind("\n", 0, m.start()) + 1
            line = APP[line_start:APP.index("\n", m.end())]
            val = m.group(1).strip()
            ok = (val == "[]"                      # the deliberate clear/repair path
                  or "if(on)" in line              # gated on the switch
                  or "relaysEnabled', true" in line   # editing the list IS choosing your own
                  # The NIP-65 seed merges the user's OWN published relay list. It is additive and
                  # — verified separately — never writes `relaysEnabled`, so it cannot turn the
                  # feature on behind them. A list that is not enabled is not used.
                  or val == "merged")
            self.assertTrue(ok, "unguarded relay-list write: " + line.strip()[:120])

    def test_the_nip65_seed_still_never_enables_the_switch(self):
        """The exemption above is only safe while that stays true."""
        body = APP[APP.index("function seedRelaysFromNip65"):]
        body = body[:body.index("\n  function ", 5)]
        self.assertIn("ClientSettings.set('relays', merged)", body)
        self.assertNotIn("relaysEnabled', true", body)


class TheRelaysButtonLandsOnTheRelays(unittest.TestCase):

    def test_it_reveals_the_control_not_just_the_screen(self):
        i = OS_JS.index("#os-net-relays")
        handler = OS_JS[i:OS_JS.index("}; }", i)]
        self.assertIn("_revealRelaySetting()", handler)

    def test_it_polls_for_the_control_rather_than_guessing_a_delay(self):
        body = OS_JS[OS_JS.index("function _revealRelaySetting(){"):]
        body = body[:body.index("\n  function ", 5)]
        self.assertIn("set-relays-on", body)
        self.assertIn("setTimeout(tick", body, "a fixed delay either stalls or misses")
        self.assertIn("scrollIntoView", body)

    def test_it_gives_up_quietly(self):
        """Landing on Settings is the old behaviour — worth no error if the control never paints."""
        body = OS_JS[OS_JS.index("function _revealRelaySetting(){"):]
        body = body[:body.index("\n  function ", 5)]
        self.assertRegex(body, r"n\s*<\s*\d+")
        self.assertNotIn("throw", body)


if __name__ == "__main__":
    unittest.main()
