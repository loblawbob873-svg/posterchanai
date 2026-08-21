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

    def js(self, body):
        src = ("const F = require(%s);\nconst out = {};\n%s\n"
               "process.stdout.write(JSON.stringify(out));" % (json.dumps(MOD), body))
        r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        return json.loads(r.stdout)

    def test_a_machine_already_in_use_is_not_seized_to_ask_a_question(self):
        """`firstRunNeeded` answers "is anything unanswered", and using that to decide whether to
        take the screen at boot is how a setup wizard stands in front of a computer somebody was
        already using. Tor is optional, an instance is optional, and this client runs signed out —
        so on a working machine every remaining step is a question, not an obstacle. Worse, the flow
        would walk them to the sign-in step, which is deliberately NOT skippable, and a desktop that
        worked as a guest a minute earlier would refuse to appear without a key."""
        # `everHadAccount` is what makes this a machine already in use rather than a fresh one: the
        # account switcher still remembers somebody, which is what signing out leaves behind.
        world = {"online": True, "instance": "https://poster.place", "pubkey": "",
                 "everHadAccount": True}
        out = self.js("out.needed = F.firstRunNeeded(%s); out.seize = F.machineUnusable(%s);"
                      % (json.dumps(world), json.dumps(world)))
        self.assertTrue(out["needed"], "Tor was never answered, so something IS unanswered")
        self.assertFalse(out["seize"],
                         "a machine with a network and an instance was seized to ask about Tor")

    def test_a_machine_nobody_has_ever_signed_into_IS_worth_interrupting(self):
        """THE LIVE DISC. It has DHCP and it ships an instance, so the old rule ("an instance OR a
        signin makes it usable") called it fine and the welcome never ran -- a fresh boot landed on
        a desktop with a sign-in prompt and no explanation. Reported as "it booted me to a desktop,
        but not logged in" and "you miss that welcome screen".

        The distinction is not a "have we run" flag, which would go stale; it is whether the account
        switcher remembers anybody, which is what signing out leaves behind."""
        fresh = {"online": True, "instance": "https://poster.place", "pubkey": "",
                 "everHadAccount": False}
        out = self.js("out.seize = F.machineUnusable(%s);" % json.dumps(fresh))
        self.assertTrue(out["seize"], "a machine nobody has ever signed into was not welcomed")

    def test_a_computer_out_of_a_box_IS_worth_interrupting(self):
        """Nothing decided — no instance, no key — is what this wizard exists for."""
        out = self.js("out.a = F.machineUnusable({online: true});"
                      "out.b = F.machineUnusable({online: false});"
                      "out.c = F.machineUnusable({netReadable: false});")
        self.assertTrue(out["a"], "a machine with nothing set up was left to fend for itself")
        self.assertTrue(out["b"], "a machine with no network was not offered one")
        self.assertTrue(out["c"], "a machine whose network could not be read was not stopped")

    def test_a_signed_in_machine_with_no_instance_is_usable(self):
        """"No instance" is a supported way to run this client, so a key alone is enough."""
        out = self.js("out.s = F.machineUnusable({online: true, pubkey: 'npub1x'});")
        self.assertFalse(out["s"])

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
        # SKIPPABLE, and it has to be now that a first boot is interrupted. The client reads the
        # public timeline signed out, so "no key yet" is a real way to use this. Left unskippable
        # while `machineUnusable` seizes a machine nobody has signed into, the welcome becomes a
        # wall across the only screen and somebody who booted a live disc to look at it could not
        # reach the desktop at all -- a worse failure than the one the interruption fixes.
        self.assertTrue(got["signin"], "a first boot could not be escaped without a key")
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


@unittest.skipIf(not NODE, "no node on this node")
class SignedInIsReadFromSomethingThatExists(unittest.TestCase):
    """`window.ME` IS NOT A THING, and readWorld asked it whether somebody was signed in.

    `ME` is a closure variable inside app.js and is never published on window, so `w.pubkey` was
    ALWAYS empty and `signin` always said "todo". It went unnoticed for as long as the rule
    short-circuited before it mattered -- `instance !== 'done' && signin !== 'done'` is false the
    moment there is an instance, whatever the signin half said. The moment signin got a say of its
    own, a machine that WAS signed in was told it was not, and the welcome came back on every boot.
    Reported as "so why do I get the welcome message again after reboot" and "i should be logged in".
    """

    def test_the_saved_session_is_what_decides(self):
        src = open(os.path.join(ROOT, "static", "js", "client", "osfirstrunui.js"),
                   encoding="utf-8").read()
        i = src.index("w.pubkey = ''")
        after = src[i:i + 800]
        self.assertIn("S.load", after,
                      "sign-in is still read from a global that does not exist")

    def test_it_does_not_rely_on_window_ME_alone(self):
        src = open(os.path.join(ROOT, "static", "js", "client", "osfirstrunui.js"),
                   encoding="utf-8").read()
        app = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
        # The premise, asserted so this cannot quietly become true again in the other direction:
        # if app.js ever DOES publish ME, the fallback below is fine either way.
        self.assertNotIn("window.ME =", app, "app.js now publishes ME — revisit the fallback")
        self.assertIn("root.ME", src, "the fallback for a build that does publish it is gone")
