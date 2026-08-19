"""Shell history as EPHEMERAL Nostr events — and the one rule that stops it leaking passwords.

Your history follows you between your own devices and is stored by nobody: ephemeral kinds
(20000-29999) are forwarded by relays and never written down. A command you ran is visible to the
terminals you have open right now, and then it is gone.

THE DANGER IS PASSWORDS AND IT IS NOT HYPOTHETICAL. A terminal's input stream carries every
keystroke, including the ones typed at `sudo`, at an ssh passphrase prompt, at `mysql -p`. Publish
the input stream and you publish those — encrypted to yourself, but published, to a relay, and into
any log that keeps them.

So a line is publishable only when the shell ECHOED it back. That is not a guess about what the text
looks like: echo is exactly what a password prompt turns off, which is why the screen stays blank
while you type one. It is the same signal the operating system uses.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "static", "js", "client", "termhist.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class Collector(unittest.TestCase):
    def run_js(self, body):
        js = "const H = require(%s);\nconst out = {};\n%s\nprocess.stdout.write(JSON.stringify(out));" \
             % (json.dumps(MOD), body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-900:])
        return json.loads(r.stdout)

    def test_an_echoed_command_is_published(self):
        out = self.run_js("""
          const c = H.makeCollector();
          c.typed('ls -la'); c.saw('ls -la');
          out.r = c.typed('\\n');
        """)
        self.assertEqual(out["r"][0]["line"], "ls -la")
        self.assertTrue(out["r"][0]["publish"])

    def test_a_password_at_a_sudo_prompt_is_not(self):
        """The whole reason this module exists. The prompt is shown, echo is off, and nothing the
        person types comes back."""
        out = self.run_js("""
          const c = H.makeCollector();
          c.saw('[sudo] password for verita84: ');
          c.typed('hunter2');
          out.r = c.typed('\\n');
        """)
        self.assertEqual(out["r"][0]["line"], "hunter2")
        self.assertFalse(out["r"][0]["publish"], "a sudo password would have been published")

    def test_an_ssh_passphrase_is_not(self):
        out = self.run_js("""
          const c = H.makeCollector();
          c.saw("Enter passphrase for key '/home/v/.ssh/id_ed25519': ");
          c.typed('correct horse battery staple');
          out.r = c.typed('\\r');
        """)
        self.assertFalse(out["r"][0]["publish"])

    def test_silence_alone_is_enough_to_withhold_it(self):
        """No prompt text at all — just a shell that stopped echoing. The rule must not depend on
        recognising the words in a prompt, because prompts are written by whoever wrote the program."""
        out = self.run_js("""
          const c = H.makeCollector();
          c.typed('s3cr3t');            /* nothing came back */
          out.r = c.typed('\\n');
        """)
        self.assertFalse(out["r"][0]["publish"])

    def test_an_echoed_secret_is_still_dropped(self):
        """Echo is necessary and not sufficient: `export TOKEN=…` echoes perfectly."""
        for line in ("export API_KEY=abcdef123456",
                     "curl -H 'Authorization: Bearer abc123' https://x",
                     "nak event --sec nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"):
            out = self.run_js("""
              const c = H.makeCollector();
              c.typed(%s); c.saw(%s);
              out.r = c.typed('\\n');
            """ % (json.dumps(line), json.dumps(line)))
            self.assertFalse(out["r"][0]["publish"], f"{line!r} was published")

    def test_a_line_that_was_backspaced_away_is_what_ran(self):
        out = self.run_js("""
          const c = H.makeCollector();
          c.typed('lss'); c.saw('lss');
          c.typed('\\x7f');            /* backspace */
          out.r = c.typed('\\n');
        """)
        self.assertEqual(out["r"][0]["line"], "ls")

    def test_ctrl_c_abandons_the_line(self):
        """A command you thought better of is not history, and it must not become the next one's
        prefix either."""
        out = self.run_js("""
          const c = H.makeCollector();
          c.typed('rm -rf /import'); c.saw('rm -rf /import');
          c.typed('\\x03');            /* ^C */
          c.typed('ls'); c.saw('ls');
          out.r = c.typed('\\n');
        """)
        self.assertEqual(len(out["r"]), 1)
        self.assertEqual(out["r"][0]["line"], "ls")

    def test_arrows_and_tabs_are_not_content(self):
        out = self.run_js("""
          const c = H.makeCollector();
          c.typed('ec'); c.saw('ec');
          c.typed('\\x1b[A');          /* an arrow key */
          c.typed('ho hi'); c.saw('ho hi');
          out.r = c.typed('\\n');
        """)
        self.assertEqual(out["r"][0]["line"], "echo hi")

    def test_an_unfinished_line_is_never_published(self):
        """It is what somebody is still typing, not something they ran."""
        out = self.run_js("""
          const c = H.makeCollector();
          c.typed('sudo rebo'); c.saw('sudo rebo');
          out.r = c.typed('');
          out.pending = c.pending();
        """)
        self.assertEqual(out["r"], [])
        self.assertEqual(out["pending"], "sudo rebo")

    def test_a_pasted_blob_is_not_history(self):
        out = self.run_js("""
          const c = H.makeCollector();
          const big = 'x'.repeat(9000);
          c.typed(big); c.saw(big);
          out.r = c.typed('\\n');
        """)
        self.assertFalse(out["r"][0]["publish"])

    def test_the_event_is_ephemeral(self):
        """20000-29999 is the ephemeral range: relays forward these and never write them down. A
        stored kind here would turn "history nobody keeps" into a permanent record of every command
        the user has ever run."""
        out = self.run_js("out.k = H.KIND; out.e = H.historyEvent('ct', 1700000000000);")
        self.assertGreaterEqual(out["k"], 20000)
        self.assertLess(out["k"], 30000)
        self.assertEqual(out["e"]["kind"], out["k"])
        self.assertEqual(out["e"]["content"], "ct", "the command must travel as ciphertext only")
        self.assertIn(["l", "pcai-shell"], out["e"]["tags"])

    def test_the_ring_merges_other_devices_by_time(self):
        """A phone that reconnects and replays five minutes of commands must not bury what you just
        ran on this machine."""
        out = self.run_js("""
          const r = H.makeRing(10);
          r.add('here-now', 300, 'desktop');
          r.add('phone-old', 100, 'phone');
          out.order = r.merged().map(x => x.line);
        """)
        self.assertEqual(out["order"], ["phone-old", "here-now"])

    def test_the_ring_drops_an_immediate_repeat_and_is_bounded(self):
        out = self.run_js("""
          const r = H.makeRing(3);
          r.add('ls', 1); r.add('ls', 2);
          out.afterRepeat = r.size();
          r.add('a', 3); r.add('b', 4); r.add('c', 5);
          out.size = r.size();
          out.lines = r.all().map(x => x.line);
        """)
        self.assertEqual(out["afterRepeat"], 1)
        self.assertEqual(out["size"], 3)
        self.assertEqual(out["lines"], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
