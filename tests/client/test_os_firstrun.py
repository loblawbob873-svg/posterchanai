"""What PosterChanOS asks on first boot, and in what order.

The order is not cosmetic. A fresh install has NO NETWORK, so every later step fails in a way that
looks like the step itself is broken: the instance picker cannot reach anything, Tor cannot
bootstrap, a remote signer cannot be contacted. Ask for wifi first and each screen is answerable;
ask for it fourth and somebody is typing an instance URL at a machine with no radio, being told the
instance is down.

The judgement worth testing is what gets SKIPPED. A step is skipped when it is already satisfied,
never when it is merely difficult — "the wifi scan failed" is not "the network is fine".
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "static", "js", "client", "osfirstrun.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class FirstRun(unittest.TestCase):
    def ask(self, world):
        js = ("const F = require(%s);\n"
              "process.stdout.write(JSON.stringify(F.nextStep(%s)));"
              % (json.dumps(MOD), json.dumps(world)))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        return json.loads(r.stdout)

    def test_a_fresh_machine_is_asked_for_the_network_first(self):
        """Everything after it needs one."""
        self.assertEqual(self.ask({})["step"], "network")

    def test_ethernet_means_there_is_nothing_to_ask(self):
        """Satisfied by ANY route out, not by wifi specifically — asking a machine already online to
        pick a network is the wizard being pleased with itself."""
        self.assertEqual(self.ask({"online": True})["step"], "instance")

    def test_a_network_that_could_not_be_ASKED_stops_the_flow(self):
        """The distinction this file exists for. NetworkManager being unreachable is not the same as
        being online, and carrying on produces three more screens of failures whose real cause was
        this one."""
        r = self.ask({"netReadable": False, "instance": "https://x", "pubkey": "ab"})
        self.assertEqual(r["step"], "network")
        self.assertTrue(r["blocked"])

    def test_a_blocked_step_wins_over_a_later_unfinished_one(self):
        r = self.ask({"netReadable": False})
        self.assertTrue(r["blocked"], r)

    def test_choosing_no_instance_is_an_answer(self):
        """The client works relay-only; "no instance" is a decision, not an omission."""
        self.assertEqual(self.ask({"online": True, "instanceSkipped": True})["step"], "tor")

    def test_declining_tor_is_an_answer_too(self):
        w = {"online": True, "instance": "https://x", "torSkipped": True}
        self.assertEqual(self.ask(w)["step"], "signin")

    def test_the_account_cannot_be_attempted_before_we_know_who_they_are(self):
        w = {"online": True, "instance": "https://x", "torSkipped": True}
        self.assertEqual(self.ask(w)["step"], "signin")
        w["pubkey"] = "abcd"
        self.assertEqual(self.ask(w)["step"], "account")

    def test_a_failed_provision_is_blocked_not_skipped(self):
        """A home directory that was not made is not a machine that is ready — the person would sign
        in successfully and then have nowhere to put anything."""
        w = {"online": True, "instance": "x", "torSkipped": True, "pubkey": "ab",
             "provisionFailed": True}
        r = self.ask(w)
        self.assertEqual(r["step"], "account")
        self.assertTrue(r["blocked"])

    def test_a_finished_machine_asks_nothing(self):
        w = {"online": True, "instance": "x", "torSkipped": True, "pubkey": "ab", "homeReady": True}
        self.assertTrue(self.ask(w).get("done"))

    def test_it_is_derived_from_the_world_not_from_a_counter(self):
        """Somebody who fixes the network outside the wizard — plugs in ethernet, walks into range —
        must not be shown the network step again. State comes from what IS, so there is no stale
        "which step were we on" to go wrong."""
        w = {"online": False}
        self.assertEqual(self.ask(w)["step"], "network")
        w["online"] = True
        self.assertEqual(self.ask(w)["step"], "instance")

    def test_only_the_genuine_choices_may_be_skipped(self):
        js = ("const F = require(%s);\nprocess.stdout.write(JSON.stringify("
              "F.STEPS.map(s => [s, F.canSkip(s)])));" % json.dumps(MOD))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        got = dict(json.loads(r.stdout))
        self.assertTrue(got["instance"])
        self.assertTrue(got["tor"])
        self.assertFalse(got["network"], "you cannot decline your way past having no network")
        self.assertFalse(got["signin"])
        self.assertFalse(got["account"], "an account is not a question")

    def test_the_network_list_matches_what_the_network_module_shows(self):
        """Two lists of the same networks that disagree about their order is worse than either."""
        js = ("const F = require(%s);\nprocess.stdout.write(JSON.stringify(F.networksForPicker(["
              "{ssid:'A',signal:40},{ssid:'A',signal:80},{ssid:'B',signal:90},"
              "{ssid:'C',signal:10,active:true}])));" % json.dumps(MOD))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        rows = json.loads(r.stdout)
        self.assertEqual([x["ssid"] for x in rows], ["C", "B", "A"])
        self.assertEqual([x for x in rows if x["ssid"] == "A"][0]["signal"], 80)


if __name__ == "__main__":
    unittest.main()
