"""THE WELCOME-SCREEN GATE MUST BE ABLE TO FAIL, AND ITS THREE ANSWERS MUST STAY THREE.

`check_livecd_vm.py` proves a graphical frame appears and stays. That passes on a desktop with no
wizard, on a stale session and on an error dialog, so it has never been evidence that an image boots
to the FIRST-RUN WIZARD -- which is the entire experience of a new machine.

`check_livecd_welcome.py` asks the running session instead, over the serial console the installer is
already driven on, by reading the verdict `osfirstrunui.js:boot()` prints. Booting an ISO needs KVM
and several minutes; the DECISION does not, and it is where the bug would be. So the judgement is
exercised here against real console text, including the case that must never read as a pass:

    "could not ask" is not "the answer was yes".

A session that never reported exits 2 (a SKIP with its reason), never 0. That distinction is the one
this codebase keeps re-learning -- an unreadable probe reported as healthy is how a false green gets
shipped.
"""
from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_livecd_welcome", ROOT / "scripts/check_livecd_welcome.py")
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def console(line):
    """Real console noise with the verdict somewhere in it, which is how it actually arrives."""
    return ("[    3.221] systemd[1]: Reached target Multi-User System.\n"
            "posterchan login: live (automatic login)\n"
            + line + "\n"
            "[    9.004] wireplumber: bluez not available\n")


class TestTheGatePasses(unittest.TestCase):
    def test_a_fresh_machine_showing_the_network_step(self):
        line = '[firstrun] showing step=network blocked=0 state={"network":"todo"}'
        self.assertEqual(MOD.judge(console(line), "iso"), 0)


class TestTheGateFails(unittest.TestCase):
    def test_a_machine_that_booted_past_the_wizard(self):
        """The failure this exists for: it boots, it is graphical, and it is unusable."""
        line = '[firstrun] skipped step=none blocked=0 state={}'
        self.assertEqual(MOD.judge(console(line), "iso"), 1)

    def test_the_wizard_opening_on_the_wrong_step(self):
        """Ask for the instance fourth and somebody types a URL at a machine with no radio."""
        line = '[firstrun] showing step=instance blocked=0 state={}'
        self.assertEqual(MOD.judge(console(line), "iso"), 1)

    def test_the_first_screen_being_a_dead_end(self):
        line = '[firstrun] showing step=network blocked=1 state={}'
        self.assertEqual(MOD.judge(console(line), "iso"), 1)


class TestCouldNotAskIsNeverAPass(unittest.TestCase):
    def test_a_session_that_said_nothing_is_a_skip(self):
        self.assertEqual(MOD.judge(console("[    9.1] nothing to report"), "iso"), 2)

    def test_a_guest_that_never_booted_is_a_skip(self):
        self.assertEqual(MOD.judge("", "iso"), 2)

    def test_a_guest_that_could_not_be_started_is_a_skip(self):
        """run_guest answers None when qemu or the firmware is missing."""
        self.assertEqual(MOD.judge(None, "iso"), 2)

    def test_the_skip_says_what_it_did_see(self):
        """'the session never started' and 'it started and said nothing' are different problems."""
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            MOD.judge(console("[    9.1] wireplumber: bluez not available"), "iso")
        out = buf.getvalue()
        self.assertIn("SKIP", out)
        self.assertIn("wireplumber", out, "the skip does not show the console it read")


class TestItReadsTheLineTheClientActuallyPrints(unittest.TestCase):
    def test_the_regex_matches_the_shipped_format(self):
        """Both halves of this contract live in different files and in different languages."""
        ui = (ROOT / "static/js/client/osfirstrunui.js").read_text(encoding="utf-8")
        self.assertIn("'[firstrun] '", ui)
        self.assertIn("' step=' +", ui)
        self.assertIn("' blocked=' +", ui)

    def test_the_verdict_words_agree(self):
        ui = (ROOT / "static/js/client/osfirstrunui.js").read_text(encoding="utf-8")
        self.assertIn("'showing' : 'skipped'", ui.replace('"', "'"))
        self.assertIn("showing", MOD.VERDICT.pattern)
        self.assertIn("skipped", MOD.VERDICT.pattern)


if __name__ == "__main__":
    unittest.main()
