"""A poll naming many tag values "since a moment ago" sorts only what can match, not every value's whole history.

2026-10-18, py-spy on server1's relay: np.unique was ~10% of its CPU. The push service polls with ONE filter naming
every push subscriber in `#p` plus `since` a few seconds back; each value's posting list is that member's entire
history of mentions, and the union sorted all of it before `since` dropped nearly everything.
"""
import random

import numpy as np

from app.services.posterchandb import store as S
from app.services.posterchandb.store import Store
from tests.test_posterchandb import mk


def test_since_cuts_the_postings_before_the_union_and_the_answer_is_the_same(tmp_path, monkeypatch):
    st = Store(str(tmp_path), flush_interval=3600, direct_durable=False)
    r = random.Random(5)
    members = ["%064x" % (i + 1) for i in range(20)]
    base = 1_700_000_000
    evs = []
    for i in range(12_000):                           # years of mentions, then a few recent ones
        t = base + i * 3600 if i < 11_950 else base + 12_000 * 3600 + r.randrange(60)
        e = mk(kind=r.choice([1, 7, 1111]), tags=[["p", r.choice(members)]], created_at=t,
               content="m%d" % i)
        st.put(e, origin="wot")
        evs.append(e)
    since = base + 12_000 * 3600
    flt = {"kinds": [1, 7, 1111], "#p": members, "_include_quotes": True, "since": since, "limit": 500}
    want = {e["id"] for e in evs if e["created_at"] >= since}

    seen = []
    real = S._union

    def spy(arrs):
        arrs = list(arrs)
        seen.append(sum(len(a) for a in arrs))
        return real(arrs)
    monkeypatch.setattr(S, "_union", spy)
    got = {e["id"] for e in st.query(flt)}
    assert got == want and len(want) == 50
    assert max(seen) <= 200, "the union was handed %d postings to keep %d" % (max(seen), len(want))

    # until, and since+until, give the same answers as a plain scan
    for f in ({"#p": members[:5], "until": base + 100 * 3600, "limit": 5000},
              {"#p": members, "since": base + 5000 * 3600, "until": base + 5100 * 3600, "limit": 5000}):
        exp = {e["id"] for e in evs if any(t[1] in f["#p"] for t in e["tags"])
               and e["created_at"] >= f.get("since", 0) and e["created_at"] <= f.get("until", 1 << 62)}
        assert {e["id"] for e in st.query(f)} == exp
    st.close()


def test_a_follows_last_week_sync_sorts_only_last_week(tmp_path, monkeypatch):
    """NEG-OPEN from a PosterChanOS local relay: {"authors": <follows>, "since": a week ago} -- exhaustive, so it used
    to union every followed author's entire history before `since` dropped nearly all of it (~9% of relay CPU)."""
    st = Store(str(tmp_path), flush_interval=3600, direct_durable=False)
    r = random.Random(9)
    authors = ["%064x" % (i + 7) for i in range(10)]
    base = 1_700_000_000
    evs = []
    for i in range(8000):
        t = base + i * 600 if i < 7980 else base + 8000 * 600 + r.randrange(60)
        e = mk(kind=r.choice([1, 6, 7]), pubkey=r.choice(authors), created_at=t, content="a%d" % i)
        st.put(e, origin="wot")
        evs.append(e)
    since = base + 8000 * 600
    seen = []
    real = S._union

    def spy(arrs):
        arrs = list(arrs)
        seen.append(sum(len(a) for a in arrs))
        return real(arrs)
    monkeypatch.setattr(S, "_union", spy)
    for flt in ({"authors": authors, "since": since}, {"authors": authors, "kinds": [1, 6, 7], "since": since}):
        seen.clear()
        got = st.match(flt)
        want = sorted(st.seq_of(e["id"]) for e in evs if e["created_at"] >= since)
        assert [int(x) for x in got] == want
        assert max(seen) <= 100, "the union was handed %d postings to keep %d" % (max(seen), len(want))
    st.close()
