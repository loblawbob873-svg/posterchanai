"""The NostrStats bot: what the numbers mean, and what the chart lets you read them off.

Run: venv-unified/bin/python -m unittest tests.test_stats_bot_windows

Reported as "Last 30 Days and Last 7 days Active Users makes no sense — Active Users looks way
higher on Last 30 days". Two separate causes, both of which made a correct measurement read as a
wrong one:

  THE LINE WAS DRAWN ON THE WRONG SCALE. It was normalised to its own maximum at 92% of the panel
  height while the only y-axis drawn was the POSTS one, so ~1,400 people read as ~9,000 and the same
  measurement appeared at a different height on each of the three panels. Both series share the left
  axis now: fewer people than posts means a line near the floor, which is the truth.

  THE LAST DAY WAS A PART-DAY. The window ended TODAY, so every chart's final bar was however much
  of today had happened — measured mid-morning UTC, 4.9k posts against a 9k run rate — and "past 7
  days" was really six days and a bit.

And the comparison the report is really about is not a bug at all: a distinct-user count over 30
days is bigger than one over 7 because a longer window catches anyone who posted once. That is why
the summary now carries the per-day average, which is the figure that means the same thing in both.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.services import stats_bot_service as S


# --- a stand-in for the relay's Postgres --------------------------------------------------------

class _Cursor:
    """Just enough psycopg2: the kind-0 fetch, the puppet fetch, and the streaming kind-1 scan."""

    def __init__(self, rows_k0, rows_k1, named=False):
        self._k0, self._k1, self._named = rows_k0, rows_k1, named
        self._mode = None
        self.itersize = 0

    def execute(self, sql, params=None):
        if "kind=0" in sql:
            self._mode = "k0"
        elif "fedi_puppets" in sql:
            raise RuntimeError("no such table")      # the common case: a node that never bridged
        else:
            self._mode = "k1"
            self._since = params[0] if params else 0

    def fetchall(self):
        return list(self._k0)

    def __iter__(self):
        return iter([(ts, pk) for ts, pk in self._k1 if ts >= self._since])

    def close(self):
        pass


class _Conn:
    def __init__(self, rows_k0, rows_k1):
        self._k0, self._k1 = rows_k0, rows_k1

    def cursor(self, name=None):
        return _Cursor(self._k0, self._k1, named=bool(name))

    def rollback(self):
        pass

    def close(self):
        pass


def _collect(rows_k1, nip05_pubkeys=("alice", "bob", "carol")):
    """Run the SHIPPED _collect_stats against a stubbed relay DB."""
    import sys
    import types
    k0 = [(pk, '{"nip05": "%s@example.com"}' % pk) for pk in nip05_pubkeys]
    fake = types.ModuleType("psycopg2")
    fake.connect = lambda *a, **k: _Conn(k0, rows_k1)
    old = sys.modules.get("psycopg2")
    sys.modules["psycopg2"] = fake
    try:
        return S._collect_stats()
    finally:
        if old is not None:
            sys.modules["psycopg2"] = old
        else:
            del sys.modules["psycopg2"]


def _utc_midnight():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


class TestTheDailyWindow(unittest.TestCase):
    def test_today_is_not_a_day_yet(self):
        """A part-day at the end of the window is a cliff on every panel and a short week in the
        text. The last bucket is YESTERDAY; anything stamped today is out of the daily numbers."""
        midnight = _utc_midnight()
        rows = [(int((midnight + timedelta(minutes=30)).timestamp()), "alice"),   # today: excluded
                (int((midnight - timedelta(hours=2)).timestamp()), "bob")]        # yesterday: counted
        st = _collect(rows)
        self.assertEqual(st["dates"][-1], (midnight - timedelta(days=1)).strftime("%m/%d"),
                         "the last daily bucket must be yesterday, not a partial today")
        self.assertEqual(sum(st["posts"]), 1, "today's post must not be in the daily buckets")
        self.assertEqual(st["active_week"], 1)

    def test_a_post_older_than_the_window_is_not_counted_twice(self):
        midnight = _utc_midnight()
        rows = [(int((midnight - timedelta(days=45)).timestamp()), "alice")]
        st = _collect(rows)
        self.assertEqual(sum(st["posts"]), 0)
        self.assertEqual(st["active_month"], 0)


class TestActiveUsers(unittest.TestCase):
    def test_active_users_are_a_union_not_a_sum(self):
        """One person posting every day is one active user, not seven."""
        midnight = _utc_midnight()
        rows = [(int((midnight - timedelta(days=d, hours=1)).timestamp()), "alice") for d in range(1, 8)]
        st = _collect(rows)
        self.assertEqual(st["active_week"], 1)
        self.assertEqual(sum(st["posts"]), 7)

    def test_the_thirty_day_count_is_allowed_to_exceed_the_seven_day_one(self):
        """The report's premise, checked: a longer window legitimately holds more distinct people.
        What was wrong was how it was DRAWN and described, not this."""
        midnight = _utc_midnight()
        rows = [(int((midnight - timedelta(days=2, hours=1)).timestamp()), "alice"),
                (int((midnight - timedelta(days=20, hours=1)).timestamp()), "bob")]
        st = _collect(rows)
        self.assertEqual(st["active_week"], 1)
        self.assertEqual(st["active_month"], 2)

    def test_the_per_day_average_is_the_comparable_figure(self):
        """Two different windows, one flat rate — which is exactly what the report could not see."""
        midnight = _utc_midnight()
        rows = []
        for d in range(1, 29):                      # alice + bob every day
            for who in ("alice", "bob"):
                rows.append((int((midnight - timedelta(days=d, hours=1)).timestamp()), who))
        st = _collect(rows)
        self.assertEqual(st["dau_avg_week"], 2)
        self.assertEqual(st["dau_avg_month"], 2)
        self.assertEqual(st["active_week"], 2)
        self.assertEqual(st["active_month"], 2)


class TestTheChartScales(unittest.TestCase):
    def test_nice_max_rounds_up_to_something_readable(self):
        for v, want in ((1400, 2000), (9300, 10000), (323100, 500000), (7, 10), (1, 1), (0, 1)):
            with self.subTest(v=v):
                self.assertGreaterEqual(S._nice_max(v), v)
                self.assertEqual(S._nice_max(v), want)

    def test_both_series_are_drawn_on_the_one_left_scale(self):
        """THE BUG, measured in pixels.

        The active-users line was normalised to its OWN maximum at 92% of the panel height while the
        only axis on the panel was the posts one — so ~1,400 people were drawn at the height of ~9,000
        posts, and the same measurement appeared at a different height on every panel. Both series
        read off the left axis now: with posts at 50-70k and users at 500-2,000, the line belongs near
        the FLOOR, and anything else is the renderer inventing a scale.
        """
        from PIL import Image
        W = H = 400
        base = Image.new("RGB", (W, H), S._BG)
        posts = [50000, 60000, 55000, 70000]
        dau = [500, 1000, 1500, 2000]
        fonts = (S._font(17), S._font(14), S._font(20))
        reg = (20, 40, W - 20, H - 20)
        S._draw_panel(base, reg, ["a", "b", "c", "d"], posts, dau,
                      S._CYAN, S._MAGENTA, "lines", "t", fonts)

        # Where did the magenta series land? Match on hue (the glow pass blends it) and only inside
        # the plot area, so the legend swatch and the value labels' outlines are not counted.
        x0, y0, x1, y1 = reg
        plot_top, plot_bottom = y0 + 6, y1 - 40
        height = plot_bottom - plot_top
        top = H
        for y in range(plot_top, plot_bottom):
            for x in range(x0 + 60, x1 - 60):
                r, g, b = base.getpixel((x, y))
                if r > 150 and b > 120 and g < 90:
                    top = min(top, y)
        self.assertLess(top, H, "the active-users series was not drawn at all")
        # 2,000 against a 70,000 axis is 3% of the height. Allow generous room for the marker and its
        # label, and still refuse anything that has been blown up to fill the panel.
        self.assertGreater(top, plot_bottom - height * 0.35,
                           "the active-users series is drawn far above where the left axis puts it — "
                           "it is being scaled by something other than the shared maximum")


if __name__ == "__main__":
    unittest.main()
