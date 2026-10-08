"""Admin -> Identities shows who signed up and never posted or did anything -- and never guesses.

"we need a way to see users that signed up on the instance and never posted or did anything" (2026-10-08).
Measured on poster.place that day: of 100 member keys, 28 had never posted and 13 had done nothing at all
(keyboard-mash test sign-ups). The rule that matters most is the could-not-ask one: when the relay's database
cannot be read, a member's activity is UNKNOWN, and an unknown row must never appear under "never posted" --
or every member reads as inactive the moment Postgres hiccups.
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from app.services import nip05_registry as N

ROOT = Path(__file__).resolve().parents[1]
A, B, C = "a" * 64, "b" * 64, "c" * 64


class _Cur:
    def __init__(self, rows): self.rows, self.sql = rows, []
    def execute(self, q, args=None): self.sql.append((q, args))
    def fetchall(self): return self.rows


class _Conn:
    def __init__(self, rows): self.cur = _Cur(rows)
    def cursor(self): return self.cur
    def close(self): pass


def test_activity_counts_posts_apart_from_what_signing_up_writes():
    conn = _Conn([(A, 12, 40, 1791400000, 1791450000, 1780000000), (B, 0, 3, None, 1791000000, 1790000000)])
    with mock.patch("psycopg2.connect", return_value=conn), \
         mock.patch("app.services.stats_bot_service._relay_dsn", return_value="x"):
        got = N.activity([A, B, C])
    assert got[A]["posts"] == 12 and got[A]["last_post"] == 1791400000
    assert got[B] == {"posts": 0, "events": 3, "last_post": None, "last_event": 1791000000, "first_seen": 1790000000}
    # A key the relay holds NOTHING for is "no activity at all" -- it was asked, and answered.
    assert got[C] == {"posts": 0, "events": 0, "last_post": None, "last_event": None, "first_seen": None}
    q, args = conn.cur.sql[-1]
    assert list(N.SIGNUP_KINDS) in args and 0 in N.SIGNUP_KINDS and 3 in N.SIGNUP_KINDS
    assert 1 in N.POST_KINDS and 0 not in N.POST_KINDS


def test_a_relay_that_cannot_be_read_is_unknown_never_inactive():
    with mock.patch("psycopg2.connect", side_effect=OSError("down")), \
         mock.patch("app.services.stats_bot_service._relay_dsn", return_value="x"):
        assert N.activity([A]) is None

    async def profiles(pks):
        return {}, True
    with mock.patch.object(N, "_names", return_value={"alice": A}), \
         mock.patch("app.services.relay_blocklist.profiles", profiles), \
         mock.patch.object(N, "activity", return_value=None):
        out = asyncio.run(N.rows("poster.place"))
    assert out["activity_complete"] is False and out["identities"][0]["activity"] is None


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_the_filters_never_list_an_unknown_row_and_say_what_they_show():
    js = r"""
    const I = require(process.argv[1]);
    const rows = {
      poster:  {activity: {posts: 4, events: 9, last_post: 1791400000, last_event: 1791450000, first_seen: 1780000000}},
      lurker:  {activity: {posts: 0, events: 3, last_post: null, last_event: 1791000000, first_seen: 1790000000}},
      ghost:   {activity: {posts: 0, events: 0, last_post: null, last_event: null, first_seen: null}},
      unknown: {activity: null},
    };
    const pick = f => Object.keys(rows).filter(k => I.passesFilter(rows[k], f));
    console.log(JSON.stringify({all: pick(''), never: pick('never_posted'), inactive: pick('inactive'),
      notes: Object.fromEntries(Object.entries(rows).map(([k, r]) => [k, I.activityNote(r)]))}));
    """
    r = subprocess.run(["node", "-e", js, str(ROOT / "static/js/admin-identities.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["all"] == ["poster", "lurker", "ghost", "unknown"]
    assert got["never"] == ["lurker", "ghost"], "an unknown row was listed as never posted"
    assert got["inactive"] == ["ghost"]
    assert got["notes"]["ghost"].startswith("never posted, no activity at all")
    assert got["notes"]["lurker"].startswith("never posted · 3 other events")
    assert got["notes"]["poster"].startswith("4 posts")
    assert "unknown" in got["notes"]["unknown"]


def test_the_filter_is_on_the_identities_panel_and_is_not_a_saved_setting():
    html = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text()
    sel = html[html.index('id="ids_filter"') - 10: html.index("</select>", html.index('id="ids_filter"'))]
    assert 'value="never_posted"' in sel and 'value="inactive"' in sel
    assert "name=" not in sel.split(">")[0], "a view filter must not be posted as a setting"
