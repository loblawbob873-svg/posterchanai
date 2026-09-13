"""Server Stats → “people active”: a pubkey is not always a person.

Run: venv-unified/bin/python -m pytest tests/test_stats_people_are_not_one_time_keys.py -q

The tile counted DISTINCT pubkeys of everything published here. Two kinds of event are signed by
something that is not a neighbour, and one of them scales with traffic:

  * kind 1059 (NIP-59 gift wrap — every DM) carries a **fresh throwaway key per message**. That is
    the wrapper's entire purpose, so the headcount grew by one for every DM ever sent.
  * kind 9735 (zap receipt) is signed by the LNURL service that settled the payment.

Measured on poster.place the day this was written: **321 “people active today”, of which 238 were
gift wraps** (real: 81), and **19,347 over 30 days, of which 16,590 were gift wraps** (real: 2,727)
— on a node with 128 registered names. A number like that is not merely wrong, it is the reason an
operator stops believing the whole panel.

The rule is pinned by RUNNING the shipped SQL expression over rows shaped like the real table, and
the second test refuses a NEW people-metric that forgets it.
"""
import re

import pytest
from sqlalchemy import create_engine, text

from app.services import stats_service as st


ROWS = []


def _add(pubkey, kind, origin="direct", n=1):
    for i in range(n):
        ROWS.append({"id": "%s-%d-%d" % (pubkey, kind, i), "pubkey": pubkey,
                     "created_at": 1_000_000 + len(ROWS), "kind": kind, "origin": origin})


# Three actual people, busy: notes, reactions, app documents.
for who in ("alice", "bob", "carol"):
    _add(who, 1, n=5)
    _add(who, 7, n=9)
    _add(who, 30078, n=40)
# 200 DMs. Every gift wrap is signed by its own one-time key — this is the inflation.
for i in range(200):
    _add("wrap%03d" % i, 1059)
# Six zaps settled by one LNURL service.
_add("lnurl-service", 9735, n=6)
# Synced content from the wider network: not this server's activity at all (origin != 'direct').
for i in range(50):
    _add("stranger%02d" % i, 1, origin="wot")


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE events (id text, pubkey text, created_at integer, "
                          "kind integer, origin text)"))
        for row in ROWS:
            conn.execute(text("INSERT INTO events VALUES (:id, :pubkey, :created_at, :kind, :origin)"),
                         row)
        yield conn


def _people(db):
    """The people count exactly as the page computes it — the shipped expression, not a copy."""
    return db.execute(text("SELECT count(DISTINCT " + st._PERSON_PUBKEY + ") FROM events "
                           "WHERE " + st._LOCAL)).scalar()


def test_a_dm_is_activity_but_not_a_new_neighbour(db):
    """200 DMs from and to the same three people are 200 one-time keys. The tile must read 3."""
    assert _people(db) == 3


def test_the_naive_count_is_what_this_replaces(db):
    """Proof the test can fail: the expression this replaced, over the same rows, reports 204 —
    one 'person' per DM plus the payment processor."""
    naive = db.execute(text("SELECT count(DISTINCT pubkey) FROM events WHERE " + st._LOCAL)).scalar()
    assert naive == 204
    assert _people(db) < naive


def test_the_payment_processor_is_not_a_visitor(db):
    """A zap receipt is signed by the LNURL service, so counting it credits the node with a person
    who has never been here."""
    assert "lnurl-service" not in {r["pubkey"] for r in ROWS if r["kind"] != 9735}
    assert _people(db) == 3          # the six receipts add nobody


def test_events_are_not_filtered_only_the_headcount_is(db):
    """DMs and zaps ARE this server's activity — the event totals must keep every one of them.
    Filtering the events too would trade one wrong number for another."""
    total = db.execute(text("SELECT count(*) FROM events WHERE " + st._LOCAL)).scalar()
    assert total == len([r for r in ROWS if r["origin"] == "direct"])
    assert total > 200


def test_no_headcount_in_this_module_may_skip_the_rule():
    """A new metric is exactly how this comes back: `count(DISTINCT pubkey)` reads like the obvious
    way to count people and is wrong everywhere in this file. Pins the RULE (every distinct-pubkey
    count goes through _PERSON_PUBKEY), not any particular call."""
    import inspect
    src = inspect.getsource(st)
    bare = [ln.strip() for ln in src.splitlines()
            if re.search(r"count\(DISTINCT\s+pubkey\s*\)", ln, re.I) and "SELECT" in ln.upper()]
    assert not bare, "a distinct-pubkey headcount that does not exclude one-time keys: %r" % bare
    assert src.count("_PERSON_PUBKEY") >= 4      # the definition + every place it is used
