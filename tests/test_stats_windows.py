"""Server Stats: "last 24h" must mean the last 24 hours.

Run: venv-unified/bin/python -m unittest tests.test_stats_windows

Reported as "there is 0 chance that only 8 memes were generated in the last 24 hours". The counter was
right and the WINDOW was wrong: counters were bucketed per UTC day, and the page served the CURRENT
day bucket under a "last 24h" label. At 20:40 in UTC-6 that is 2.7 hours of activity — 8 memes against
a 7-day average of 51/day. Worst immediately after the UTC rollover, which is 18:00 local here, so the
numbers collapsed every evening and looked broken.

The fix is a parallel hourly bucket. What is asserted here is the property the day buckets could not
have: a window that SPANS the UTC midnight boundary still counts what happened on the other side of it.
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from app.services import settings_store as ss
from app.services import stats_service as st


class _Store:
    """stats_service against a throwaway counter file, so a test never touches the node's real
    counters (they are local-only JSON, and this suite bumps them by design)."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="pcstats-")
        self.path = os.path.join(self.dir, "local_settings.json")
        with open(self.path, "w") as f:
            json.dump({}, f)
        self.p = mock.patch.object(ss, "_LOCAL_PATH", self.path)
        self.p.start()
        return self

    def __exit__(self, *a):
        self.p.stop()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def at(self, when, metric, n=1):
        """Record `n` of `metric` as though it happened at unix time `when`."""
        g = time.gmtime(when)
        ss.bump_counter(st._COUNTER_KEY, time.strftime("%Y-%m-%d", g), metric, n)
        ss.bump_counter(st._COUNTER_KEY_H, time.strftime("%Y-%m-%dT%H", g), metric, n)


class RollingWindow(unittest.TestCase):
    # A moment deliberately just after UTC midnight — the exact condition under which the old code
    # under-reported, and 20:40 local for a UTC-6 operator.
    NOW = 1785638400          # 2026-08-02 02:40 UTC

    def test_bump_writes_both_buckets(self):
        with _Store():
            st.bump("meme")
            day = ss.read_counter(st._COUNTER_KEY)
            hour = ss.read_counter(st._COUNTER_KEY_H)
            self.assertTrue(any(v.get("meme") for v in day.values()), "daily bucket not written")
            self.assertTrue(any(v.get("meme") for v in hour.values()), "hourly bucket not written")

    def test_last24_counts_across_the_utc_midnight_boundary(self):
        """THE bug. 5 memes before UTC midnight and 8 after: the day bucket sees 8, a real 24h
        window sees 13."""
        with _Store() as s:
            for i in range(8):
                s.at(self.NOW - 600 * i, "meme")          # within the last ~80 min (after midnight)
            for i in range(5):
                s.at(self.NOW - 3600 * (4 + i), "meme")   # 4-8h ago, i.e. YESTERDAY in UTC
            out = st._counter_series(self.NOW)
            m = out["metrics"]["meme"]
            self.assertEqual(m["today"], 8, "the day bucket only ever saw today's 8")
            self.assertEqual(m["last24"], 13, "a real 24h window must include the 5 from before midnight")

    def test_last1h_is_the_current_hour_only(self):
        with _Store() as s:
            s.at(self.NOW, "image", 3)
            s.at(self.NOW - 7200, "image", 9)             # two hours ago
            m = st._counter_series(self.NOW)["metrics"]["image"]
            self.assertEqual(m["last1h"], 3)
            self.assertEqual(m["last24"], 12)

    def test_events_older_than_the_window_fall_out(self):
        with _Store() as s:
            s.at(self.NOW, "music", 2)
            s.at(self.NOW - 30 * 3600, "music", 40)       # 30h ago — outside 24h
            m = st._counter_series(self.NOW)["metrics"]["music"]
            self.assertEqual(m["last24"], 2)
            self.assertEqual(m["total"], 42, "all-time still counts it")

    def test_rolling_is_false_until_the_window_is_actually_covered(self):
        """Hourly counting starts when this ships, so for the first day the window is mostly empty.
        Publishing it then would swap a mislabelled-but-real number for a confident 0 — worse than the
        bug. The flag stays false until the store reaches back a full 24h, and the client relabels."""
        with _Store() as s:
            s.at(self.NOW - 3600, "meme")                 # only an hour of history
            self.assertFalse(st._counter_series(self.NOW)["rolling"])
            s.at(self.NOW - 26 * 3600, "meme")            # now it reaches past 24h
            self.assertTrue(st._counter_series(self.NOW)["rolling"])

    def test_an_empty_store_is_not_claimed_as_covered(self):
        with _Store():
            out = st._counter_series(self.NOW)
            self.assertFalse(out["rolling"])
            self.assertEqual(out["metrics"]["meme"]["last24"], 0)


if __name__ == "__main__":
    unittest.main()
