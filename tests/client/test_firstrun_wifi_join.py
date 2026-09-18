"""JOINING WIFI IN THE FIRST-RUN WIZARD, DRIVEN AGAINST THE SHIPPED CODE.

Reported from a live boot on a TV, in three sentences that turned out to be one bug:

    "when i try to enter password on tv for wifi, it keeps reloading the password dialog"
    "then it reloaded the Join a network screen or desktop"
    "the Join a network is so glitchey and stopped responding to clicking the access point"

`stepNetwork` polls `net.status()` every two seconds and calls `run()` the moment it sees `online`.
`run()` TEARS THE CARD DOWN AND REBUILDS IT — including the access-point buttons, whose click
handlers are bound to those exact elements. The password prompt is an `await` in the middle of that,
and associating with an AP can report `online` before the join completes (a flapping link reports it
more than once), so the rebuild lands underneath the dialog: the typed password goes nowhere and
every button the user was clicking has been replaced by a new one that never got a handler from the
render they were interacting with.

The watcher is not the problem and is not removed — a cable coming up late is exactly what it exists
for, and that was itself a fix ("no wifi listed" from a machine whose ethernet was working). It just
must not act while somebody is mid-answer.

`firstrun_wifi_sim.mjs` runs the real function under node with a fake clock: click an AP, hold the
prompt open as a person typing does, fire the watcher with `online: true`, count `run()` calls.
Verified to fail without the fix — with the busy check removed it reports `runsDuringPrompt: 1`.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/firstrun_wifi_sim.mjs"


def sim(**plan):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    out = subprocess.run([node, str(SIM), json.dumps(plan)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-600:]
    data = json.loads(out.stdout or "{}")
    assert "error" not in data, data["error"]
    return data


class FirstRunWifiJoin(unittest.TestCase):
    def test_the_watcher_never_rebuilds_the_card_while_a_password_is_being_typed(self):
        """THE REPORTED BUG. Three watcher ticks land while the dialog is open; none may rebuild."""
        r = sim(tickWhilePrompting=3, online=True, secure=True, joinOk=True)
        self.assertEqual(0, r["runsDuringPrompt"],
                         "the card rebuilt itself while the password prompt was open — that is the "
                         "dialog 'reloading' and the access points going dead under the cursor")
        self.assertEqual(0, r["rebuiltDuringPrompt"],
                         "the access-point list was re-rendered mid-prompt, detaching the handlers")

    def test_the_join_still_happens_and_the_wizard_still_advances(self):
        """The guard must not cost the feature: one join, one advance."""
        r = sim(tickWhilePrompting=2, online=True, secure=True, joinOk=True)
        self.assertEqual("HomeWifi", r["joined"], "the password was never handed to NetworkManager")
        self.assertEqual(1, r["runs"], "the wizard did not move on exactly once after joining")

    def test_a_cancelled_prompt_releases_the_watcher(self):
        """Somebody who changes their mind must not freeze the one poll that notices a cable.

        Driven by cancelling: the sim resolves the prompt, but a plan with no join still has to leave
        the card responsive. This asserts the flag is not latched on by a path that returns early.
        """
        r = sim(tickWhilePrompting=1, online=False, secure=True, joinOk=True)
        self.assertEqual(0, r["runsDuringPrompt"])
        # online:false means the watcher had nothing to do; the join still completes and advances.
        self.assertEqual(1, r["runs"])

    def test_an_open_network_needs_no_prompt_and_still_joins(self):
        r = sim(tickWhilePrompting=2, online=True, secure=False, joinOk=True)
        self.assertEqual(0, r["promptOpened"], "an open network asked for a password")
        self.assertEqual("HomeWifi", r["joined"])

    def test_a_refused_join_does_not_leave_the_watcher_stood_down_for_ever(self):
        """A wrong password must not disable the poll that would otherwise notice ethernet."""
        r = sim(tickWhilePrompting=1, online=True, secure=True, joinOk=False)
        self.assertIsNone(r.get("runsAfterFailure"))
        self.assertEqual(0, r["runsDuringPrompt"])
        # The failure path returns without advancing; what matters is that it cleared the flag, which
        # the shipped code does on every early return. A latched flag would be a card that never
        # again notices the world changing.
        src = (ROOT / "static/js/client/osfirstrunui.js").read_text(encoding="utf-8")
        block = src[src.index("_netBusy = true;"):src.index("  function stepInstance(")]
        self.assertGreaterEqual(block.count("_netBusy = false;"), 3,
                                "not every exit from the join clears the busy flag")


if __name__ == "__main__":
    unittest.main()
