"""Admin -> Identities shows who signed up and never posted or did anything -- and never guesses.

"we need a way to see users that signed up on the instance and never posted or did anything" (2026-10-08).
Measured on poster.place that day: of 100 member keys, 28 had never posted and 13 had done nothing at all
(keyboard-mash test sign-ups). The rule that matters most is the could-not-ask one: when the relay's database
cannot be read, a member's activity is UNKNOWN, and an unknown row must never appear under "never posted" --
or every member reads as inactive the moment the relay hiccups. Activity is asked of THIS node's relay (#161), never
read from its Postgres tables.
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from app.services import nip05_registry as N
from tests.relay_fake import FakeRelay, ev

ROOT = Path(__file__).resolve().parents[1]
A, B, C = "a" * 64, "b" * 64, "c" * 64


@pytest.fixture
def relay(monkeypatch):
    """THIS node's relay (#161: activity is asked of the relay, never read from its Postgres tables)."""
    r = FakeRelay()
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(r.port))
    yield r
    r.close()


def _no_database(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("activity opened a database connection; it must ask the relay")
    monkeypatch.setattr("psycopg2.connect", refuse)


def test_activity_counts_posts_apart_from_what_signing_up_writes(relay, monkeypatch):
    _no_database(monkeypatch)
    D = "d" * 64
    relay.events += [ev(A, 0, 1780000000), ev(A, 3, 1785000000)]                          # signing up
    relay.events += [ev(A, 1, 1791400000 - i) for i in range(12)]                          # 12 posts
    relay.events += [ev(A, 5, 1791450000 - i) for i in range(28)]                          # 28 other events
    relay.events += [ev(B, 0, 1790000000), ev(B, 10002, 1791100000)]
    relay.events += [ev(B, 5, 1791000000 - i) for i in range(3)]
    # D: one deletion, then a page's worth of PRIVATE documents newer than it -- the relay withholds them, so
    # the newest-first page shows nothing; the date must still be found, and must not be one of theirs.
    relay.events += [ev(D, 0, 1700000000), ev(D, 5, 1760000000)]
    relay.events += [ev(D, 30078, 1770000000 + i, tags=[["d", "pcai:note:%d" % i]]) for i in range(10)]
    got = N.activity([A, B, C, D])
    assert got[A]["posts"] == 12 and got[A]["last_post"] == 1791400000
    assert got[A] == {"posts": 12, "events": 40, "last_post": 1791400000, "last_event": 1791450000,
                      "first_seen": 1780000000}
    assert got[B] == {"posts": 0, "events": 3, "last_post": None, "last_event": 1791000000, "first_seen": 1790000000}
    # A key the relay holds NOTHING for is "no activity at all" -- it was asked, and answered.
    assert got[C] == {"posts": 0, "events": 0, "last_post": None, "last_event": None, "first_seen": None}
    assert got[D] == {"posts": 0, "events": 1, "last_post": None, "last_event": 1760000000, "first_seen": 1700000000}
    assert list(N.SIGNUP_KINDS) and 0 in N.SIGNUP_KINDS and 3 in N.SIGNUP_KINDS
    assert 1 in N.POST_KINDS and 0 not in N.POST_KINDS


def test_a_relay_that_cannot_be_read_is_unknown_never_inactive(monkeypatch):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(port))
    assert N.activity([A]) is None

    async def profiles(pks):
        return {}, True
    with mock.patch.object(N, "_names", return_value={"alice": A}), \
         mock.patch("app.services.relay_blocklist.profiles", profiles), \
         mock.patch.object(N, "activity", return_value=None):
        out = asyncio.run(N.rows("poster.place"))
    assert out["activity_complete"] is False and out["identities"][0]["activity"] is None


def test_a_count_the_relay_refuses_is_unknown_not_zero(relay, monkeypatch):
    """One unanswered COUNT makes the whole answer unknown -- a missing count read as 0 would list a poster
    under "never posted"."""
    relay.events += [ev(A, 1, 1791400000)]
    from app.services import relay_reader

    def refused(filters, **k):
        raise relay_reader.Unavailable("the relay refused a COUNT: rate-limited")
    monkeypatch.setattr(relay_reader, "counts", refused)
    assert N.activity([A]) is None


def test_more_members_than_one_req_may_carry_are_all_answered(relay):
    """The relay keeps ten filters per REQ and drops the rest silently; a member past the tenth must not read
    as never having posted."""
    pks = ["%064x" % (i + 1) for i in range(23)]
    for i, pk in enumerate(pks):
        relay.events += [ev(pk, 0, 1700000000 + i), ev(pk, 1, 1790000000 + i)]
    got = N.activity(pks)
    for i, pk in enumerate(pks):
        assert got[pk]["last_post"] == 1790000000 + i, i
        assert got[pk]["first_seen"] == 1700000000 + i, i


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
