"""Shell history as ephemeral Nostr events — the rules, run, and the wiring that was missing.

`termhist.js` names this file in its own header and this file did not exist. That is not a
bookkeeping detail: the module was also called by NOTHING. It was loaded by client.html, precached
by the service worker, and no line of any other file mentioned it — a terminal with a history
feature that had never once run. So the last test here is about the WIRING, and it is the one that
would have caught it.

The rest is the rule that makes the feature publishable at all: a terminal's input stream carries
every keystroke, including the ones typed at `sudo`, at an ssh passphrase, at `mysql -p`. A line is
publishable only when the shell ECHOED it back, because echo is exactly what a password prompt
turns off — the same signal the operating system uses, not a guess about what the text looks like.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "static", "js", "client", "termhist.js")
TERM = os.path.join(ROOT, "static", "js", "client", "term.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "needs node")
class HistoryRules(unittest.TestCase):
    def js(self, body):
        src = ("const H = require(%s);\nconst out = {};\n%s\n"
               "process.stdout.write(JSON.stringify(out));" % (json.dumps(MOD), body))
        r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-900:])
        return json.loads(r.stdout)

    # ---- the password rule -------------------------------------------------------------------
    def test_an_echoed_command_is_publishable(self):
        out = self.js("""
          const c = H.makeCollector();
          c.typed('ls -la'); c.saw('ls -la');
          out.lines = c.typed('\\r');
        """)
        self.assertEqual([l["line"] for l in out["lines"]], ["ls -la"])
        self.assertTrue(out["lines"][0]["publish"])

    def test_a_password_is_NOT_published_because_nothing_echoed_it(self):
        """The whole feature rests on this. A password prompt turns echo off — the screen stays
        blank while you type — so a line the shell never echoed is a line this must not publish."""
        out = self.js("""
          const c = H.makeCollector();
          c.saw('[sudo] password for someone: ');
          c.typed('hunter2correcthorse');
          out.lines = c.typed('\\r');
        """)
        self.assertEqual(len(out["lines"]), 1)
        self.assertFalse(out["lines"][0]["publish"],
                         "a password typed at a prompt with echo off was marked publishable")
        self.assertIn("echo", out["lines"][0]["why"])

    def test_an_echoed_secret_is_still_refused(self):
        """Echo is necessary and not sufficient: `export TOKEN=…` is echoed perfectly and is still
        a secret."""
        out = self.js("""
          const c = H.makeCollector();
          const t = 'export AWS_SECRET_ACCESS_KEY=abcdef123456';
          c.typed(t); c.saw(t);
          out.lines = c.typed('\\r');
        """)
        self.assertFalse(out["lines"][0]["publish"])
        self.assertIn("secret", out["lines"][0]["why"])

    def test_a_line_that_was_never_run_is_not_a_command(self):
        """Half a command, still being typed, is not history — only Enter makes one."""
        out = self.js("""
          const c = H.makeCollector();
          c.typed('rm -rf /import'); c.saw('rm -rf /import');
          out.pending = c.pending();
          out.lines = c.typed('\\u0003');      // ^C — abandoned
          out.after = c.pending();
        """)
        self.assertEqual(out["pending"], "rm -rf /import")
        self.assertEqual(out["lines"], [])
        self.assertEqual(out["after"], "")

    def test_an_arrow_key_does_not_end_up_in_the_command(self):
        """An arrow is ESC [ A — three bytes, of which only ESC is a control character. Consuming
        the control byte alone leaves "[A" in the line, so pressing ↑ mid-command publishes a
        command nobody typed."""
        out = self.js("""
          const c = H.makeCollector();
          c.typed('git status'); c.saw('git status');
          c.typed('\\u001b[A');                 // ↑
          out.pending = c.pending();
          out.lines = c.typed('\\r');
        """)
        self.assertEqual(out["pending"], "git status")
        self.assertEqual(out["lines"][0]["line"], "git status")

    # ---- the ring ----------------------------------------------------------------------------
    def test_another_devices_replay_does_not_bury_what_you_just_ran(self):
        """A phone that reconnects and replays five minutes of commands arrives all at once. The
        list is ordered by WHEN A COMMAND WAS RUN, never by when its event turned up."""
        out = self.js("""
          const r = H.makeRing(50);
          r.add('mine', 5000, '');
          r.add('theirs-old', 1000, 'phone');
          r.add('theirs-new', 3000, 'phone');
          out.order = r.merged().map(x => x.line);
        """)
        self.assertEqual(out["order"], ["theirs-old", "theirs-new", "mine"])

    def test_the_same_command_twice_in_a_row_is_one_entry(self):
        out = self.js("""
          const r = H.makeRing(50);
          r.add('ls', 1, ''); r.add('ls', 2, ''); r.add('pwd', 3, '');
          out.n = r.size();
        """)
        self.assertEqual(out["n"], 2)

    def test_the_event_is_ephemeral_so_no_relay_writes_it_down(self):
        """20000–29999 is forwarded and never stored. That is the design — not a synced document
        somebody would then have to think about deleting."""
        out = self.js("out.k = H.KIND; out.e = H.historyEvent('ciphertext', 1700000000000);")
        self.assertGreaterEqual(out["k"], 20000)
        self.assertLess(out["k"], 30000)
        self.assertEqual(out["e"]["kind"], out["k"])
        self.assertEqual(out["e"]["content"], "ciphertext")
        self.assertEqual(out["e"]["created_at"], 1700000000)

    # ---- the wiring --------------------------------------------------------------------------
    def test_the_terminal_actually_CALLS_it(self):
        """THE ONE THAT MATTERS. This module shipped complete, loaded by the page, precached by the
        service worker, named in a test file that did not exist — and called by nothing at all. A
        terminal had a shared-history feature that had never run once, and every angle except this
        one said it was finished.

        Both directions are asserted, because either alone is silently useless: keystrokes with no
        output means nothing is ever echoed, so nothing is ever publishable; output with no
        keystrokes means there is nothing to echo."""
        src = open(TERM, encoding="utf-8").read()
        self.assertIn("PCTermHistory", src,
                      "term.js does not reach for the history module at all")
        self.assertTrue(re.search(r"term\.onData\(d\s*=>\s*\{[^}]*_histTyped\(d\)", src),
                        "keystrokes are not fed to the collector — nothing can ever be a command")
        # term.write may have a completion callback (used to pin the live prompt after xterm has
        # laid out the new bytes). History still has to observe the same bytes immediately after
        # that write is scheduled; requiring the old one-argument spelling made the test reject a
        # functioning collector whenever terminal scrolling was improved.
        self.assertTrue(re.search(r"term\.write\(m\.d(?:,[\s\S]{0,300}?)?\);\s*\n\s*_histSaw\(m\.d\)", src),
                        "the shell's output is not fed to the collector — nothing is ever echoed, "
                        "so the echo rule refuses every line and the history stays empty")
        # And it must be encrypted to the user's own key, never published in the clear.
        self.assertIn("nip44enc", src, "history is published without being encrypted")
        # An ephemeral event replayed ten minutes late is not history, it is noise.
        self.assertIn("noQueue", src, "history joins the offline Outbox and is replayed late")


if __name__ == "__main__":
    unittest.main()
