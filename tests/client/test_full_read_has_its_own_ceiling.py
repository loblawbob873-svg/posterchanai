"""A full record read is megabytes, and it used to share a twenty-second ceiling with a 2 kB POST.

Run: venv-unified/bin/python -m pytest tests/client/test_full_read_has_its_own_ceiling.py

MEASURED, on the real folder this was reported from: 12,436 records answer in **6.15 MB** (4.03 MB
gzipped), of which the server spends 1.2s reading the relay — the rest is serialising and sending
it through one uvicorn worker that several devices are asking at once.

Why losing that race is not merely slow: the read is ALL OR NOTHING. A device that times out caches
nothing, and a device with no cache can only ask for the full set again — which times out again.
One missed read is a device that can never sync, and it spreads: every device that joins fresh, or
whose era moved, lands in the same loop. Reported as "could not read the folder's shared record …
this is affecting every device now", with the server answering 200 OK to every one of those
requests, because it did answer — later than the client was willing to wait.

This ceiling is a stopgap and says so in the source: the real fix is paging the read so a device
keeps what it got. What is asserted here is the shape that must not come back — one constant
covering both kinds of request — and that the ceiling survives the token-retry, which was the easy
half to miss (a retried full read dropping back to 20s fails exactly like the original).
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNC = os.path.join(ROOT, "static", "js", "client", "sync.js")


class AFullReadIsNotASmallPost(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(SYNC, encoding="utf-8").read()

    def _fn(self, name):
        i = self.src.index(name)
        depth, k = 0, self.src.index("{", i)
        while k < len(self.src):
            if self.src[k] == "{":
                depth += 1
            elif self.src[k] == "}":
                depth -= 1
                if depth == 0:
                    return self.src[i:k + 1]
            k += 1
        raise AssertionError(f"{name}: unbalanced braces")

    def test_there_is_a_separate_ceiling_and_it_is_generous(self):
        m = re.search(r"const _FULL_READ_TIMEOUT_MS = ([^;]+);", self.src)
        self.assertTrue(m, "the full-read ceiling is gone — a 6 MB read is back on the 20s bound")
        ms = eval(m.group(1).replace(" ", ""))          # e.g. 4 * 60 * 1000
        self.assertGreaterEqual(ms, 60_000,
                                "under a minute is not a ceiling for a multi-megabyte read on a "
                                "phone; giving up costs more than waiting here")

    def test_the_post_helper_takes_the_ceiling_from_its_caller(self):
        fn = self._fn("async function _statePost(")
        self.assertIn("async function _statePost(body, _retry, ms){", fn)
        self.assertIn("const bound = ms || _POST_TIMEOUT_MS;", fn)
        self.assertNotIn("}, _POST_TIMEOUT_MS);", fn,
                         "the abort timer or _bounded still hard-codes the small ceiling")

    def test_both_bounds_move_together(self):
        """There are TWO of them — an AbortController timer and _bounded — and a request bounded by
        only one of them is bounded by the smaller."""
        fn = self._fn("async function _statePost(")
        self.assertIn("ctl.abort(); }catch(_){} }, bound);", fn)
        self.assertIn("'server', bound);", fn)

    def test_the_token_retry_keeps_it(self):
        fn = self._fn("async function _statePost(")
        self.assertIn("return _statePost(body, true, ms);", fn,
                      "a retried full read drops back to 20s and fails the same way")

    def test_the_reader_asks_for_it_only_when_the_read_is_full(self):
        """`since` is present exactly when this device has a cache to read forward from, so its
        absence IS the full read. A delta keeps the short ceiling: it is a few kB and a device
        waiting four minutes for one is a hang."""
        fn = self._fn("async load(key, onTick){")
        self.assertIn("body.since ? _POST_TIMEOUT_MS : _FULL_READ_TIMEOUT_MS", fn)
        self.assertIn("if(cache && cache.cursor && cache.fullAt", fn,
                      "the delta condition moved — re-check which requests are full reads")
        self.assertIn("(Date.now() - cache.fullAt) < this._FULL_REANCHOR_MS", fn,
                      "a stale cache must re-anchor with a full read rather than delta forever")


if __name__ == "__main__":
    unittest.main()
