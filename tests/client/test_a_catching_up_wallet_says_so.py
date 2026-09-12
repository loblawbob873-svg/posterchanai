"""A Monero wallet that is reading blocks must not be reported as one that died.

Reported as: "why am I seeing 'The wallet did not answer' when accessing my monero wallet on
desktop! wtf is going on with this shit desktop".

Nothing was wrong with the wallet. Measured on the node at the time of the report:

    sync_state -> {'checked': True, 'scanning': True, 'blocks_fetched': 1}
    balance    -> 0.0181844623 XMR

The wallet was catching up. A catching-up wallet is SLOW, so the client's probe hit its deadline
and painted the `TIMED_OUT` card — "The wallet did not answer" — which describes a wallet that is
gone. There is a `busyHtml()` card that says exactly the right thing ("Wallet is catching up…
nothing is lost, and tipping still works through your own wallet meanwhile"), and it was never
reached.

Two separate causes, both of them the same shape — two surfaces with different vocabulary for one
state:

1. `busy` was decided by matching the error text against three phrases, and the node says a FOURTH:
   "The wallet is not caught up with the Monero network yet".
2. A timed-out probe never asked WHY it was slow, though `/sync-state` is a cheap route that exists
   for exactly that question.
"""
import re
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "static/js/client/monero-wallet.js").read_text()
SERVER = (Path(__file__).resolve().parents[2] / "app/services/monero_wallet_service.py").read_text()


class TheTwoSidesShareAVocabulary(unittest.TestCase):
    def test_the_clients_busy_test_covers_the_message_the_server_actually_sends(self):
        """The bug, stated as a rule: every phrase the node uses for 'still syncing' must be one the
        client recognises. Read the SERVER's wording rather than trusting a copy of it."""
        m = re.search(r'WalletError\("(The wallet is not caught up[^"]*)"', SERVER)
        self.assertTrue(m, "the node no longer has a 'not caught up' error — update this test")
        server_phrase = m.group(1)

        pattern = re.search(r"busy:\s*/([^/]+)/i", SRC)
        self.assertTrue(pattern, "the client no longer decides `busy` from the message")
        self.assertTrue(re.search(pattern.group(1), server_phrase, re.I),
                        f"the client's busy test does not match the node's own wording "
                        f"({server_phrase!r}) — a syncing wallet reads as a dead one")

    def test_the_older_phrases_still_match(self):
        pattern = re.search(r"busy:\s*/([^/]+)/i", SRC).group(1)
        for phrase in ("the wallet did not answer in time", "the wallet is busy",
                       "still reading the chain"):
            self.assertTrue(re.search(pattern, phrase, re.I), phrase)


class ASlowProbeAsksWhy(unittest.TestCase):
    def test_a_timeout_checks_whether_the_wallet_is_scanning(self):
        block = SRC[SRC.index("if(s === TIMED_OUT){"):]
        block = block[:block.index("return;\n    }")]
        self.assertIn("sync-state", block,
                      "a timed-out probe still calls the wallet dead without asking why it was slow")
        self.assertIn("st.scanning", block)
        self.assertIn("busyHtml()", block,
                      "a scanning wallet is not shown the 'catching up' card that exists for it")

    def test_the_question_is_itself_bounded(self):
        """Asking why must not hang the screen that is already complaining about a hang."""
        block = SRC[SRC.index("if(s === TIMED_OUT){"):]
        block = block[:block.index("return;\n    }")]
        self.assertIn("Promise.race", block)
        self.assertIn("setTimeout", block)

    def test_the_dead_card_is_still_there_for_an_actually_dead_wallet(self):
        self.assertIn("The wallet did not answer", SRC)


if __name__ == "__main__":
    unittest.main()
