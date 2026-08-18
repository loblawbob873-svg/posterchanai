"""A refused same-second update retries one second ahead instead of losing the write.

NIP-01 settles a created_at tie by LOWER id — right for cross-relay convergence, wrong as the
answer to "save my newer version": ids are random, so a journal batching several publishes per
second lost ~half of them to "not stored, retry" (measured: six refusals in one second on a real
account mid-sweep, surfacing as "relay rejected the write, not saved" on the card)."""
import asyncio
import unittest
from unittest import mock

from app.services import nostr_store


class TieRetry(unittest.TestCase):
    def _run(self, answers):
        calls = []
        async def fake_publish(port, ev):
            calls.append(int(ev["created_at"]))
            return answers[min(len(calls) - 1, len(answers) - 1)]
        with mock.patch.object(nostr_store, "_ws_publish", side_effect=fake_publish):
            ok = asyncio.run(nostr_store.put_doc(3052, b"\x01" * 32, "pcai:test:doc",
                                                 {"a": 1}, encrypt=False))
        return ok, calls

    def test_a_tie_refusal_retries_with_a_bumped_second(self):
        ok, calls = self._run([(False, "error: not stored, retry"), (True, "")])
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], calls[0] + 1, "the retry did not move created_at forward")

    def test_a_hard_refusal_is_not_retried(self):
        ok, calls = self._run([(False, "blocked: not in web of trust")])
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1, "a WoT refusal was hammered with retries")

    def test_the_retry_is_bounded(self):
        ok, calls = self._run([(False, "error: not stored, retry")] * 10)
        self.assertFalse(ok)
        self.assertLessEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()
