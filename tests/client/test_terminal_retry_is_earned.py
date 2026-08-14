"""A reconnect only counts once the connection has HELD, and a teardown always says so.

THE BUG THIS PINS. `ready` means the server accepted the socket, not that it works — and the failure
that cost an afternoon happened immediately after it, when a 401 KB replay frame killed the relay.
Resetting `retry` on `ready` therefore made every cycle reattach → ready → retry=0 → die → reattach,
so the backoff never climbed, "gave up reconnecting — press Connect" was UNREACHABLE, and the whole
thing was silent: no console error, nothing on screen, and a server log cheerfully saying
"reattached … via the keeper" each time round.

Read out of the shipped term.js rather than reimplemented, because the thing being asserted is a
property of that file — a rewrite here would keep passing after the file drifted.
"""
import re
import unittest
from pathlib import Path

TERM = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "term.js"


class TerminalRetryIsEarned(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = TERM.read_text(encoding="utf-8")

    def test_ready_does_not_reset_the_retry_counter_on_its_own(self):
        """The single line that made a broken terminal indistinguishable from an idle one."""
        ready = self.src.split("if(m.t === 'ready')", 1)
        self.assertEqual(len(ready), 2, "the ready handler moved; this test needs updating")
        # Everything up to the end of that handler block, with the comments and the deferred timer
        # body removed — a reset INSIDE the timer is the fix, and prose describing the old bug is
        # not code. What must not survive is a bare reset that runs the moment `ready` arrives.
        block = ready[1].split("return;", 1)[0]
        block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
        block = re.sub(r"//[^\n]*", "", block)
        block = re.sub(r"setTimeout\(.*?\}\s*,\s*\d+\)", "", block, flags=re.S)
        self.assertNotRegex(
            block, r"retry\s*=\s*0",
            "`ready` must not reset retry immediately: a socket that dies straight after it would "
            "loop for ever and the give-up branch would never be reached")

    def test_the_counter_is_reset_only_after_the_socket_has_held(self):
        self.assertIn("provenT", self.src, "there is no 'has it held?' timer at all")
        m = re.search(r"provenT\s*=\s*setTimeout\(function\(\)\{[^}]*retry\s*=\s*0[^}]*\},\s*(\d+)\)",
                      self.src)
        self.assertIsNotNone(m, "retry is not reset from the proven-connection timer")
        held = int(m.group(1))
        self.assertGreaterEqual(held, 2000,
                                "too short to distinguish a working socket from one that dies on "
                                "its first frame, which is the failure this exists for")

    def test_the_timer_is_cancelled_when_the_socket_goes(self):
        """Left running, it would vouch for a connection that had already died — which is the bug
        again, one level down."""
        for fn in ("_drop", "_bye"):
            body = self.src.split("function " + fn + "(", 1)
            self.assertEqual(len(body), 2, f"{fn} not found")
            self.assertIn("clearTimeout(provenT)", body[1].split("\n    }", 1)[0],
                          f"{fn} does not cancel the proven-connection timer")

    def test_the_give_up_message_is_still_reachable(self):
        self.assertIn("gave up reconnecting", self.src)
        self.assertRegex(self.src, r"retry\s*>\s*\d+",
                         "nothing bounds the retries, so the give-up branch can never fire")

    def test_a_session_ending_is_never_silent(self):
        """`closed_reason` is empty for most ordinary endings, so `if(m.m)` tore the session down
        saying nothing at all — the screen just stopped being a terminal."""
        end = self.src.split("if(m.t === 'end')", 1)
        self.assertEqual(len(end), 2, "the end handler moved; this test needs updating")
        block = end[1][:1200]
        self.assertNotRegex(block, r"if\(m\.m\)\s*_state",
                            "an `end` with no reason must still say something")
        self.assertRegex(block, r"m\.m\s*\|\|",
                         "there is no fallback wording for an end that carries no reason")


if __name__ == "__main__":
    unittest.main()
