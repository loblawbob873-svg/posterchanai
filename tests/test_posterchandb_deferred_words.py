"""Search words are indexed AFTER a copy or a replay -- and every search answer stays exact.

Measured on 200k real events: storing cost 365 us an event and 93% of it was the Postgres-exact search tokenizer,
so copying the relay (2.65M events) took ~18 min while everything else about it took ~3. A copy and a log replay
now defer the words; whatever queries the store with a `search` before they are indexed gets them indexed first
(exact, just slower), the mirror sends those searches to Postgres and indexes in the background, and a snapshot
is only ever taken of a COMPLETE index.
"""
import random

from app.services.posterchandb import snapshot as snap
from app.services.posterchandb.store import Store
from tests.test_posterchandb_durability import _events
from tests.test_posterchandb_tsparser import FRAG


def _with_text(seed, n):
    r = random.Random(seed)
    evs = _events(seed, n)
    for e in evs:                         # real-looking words, so searches hit (ids stay valid: only the store sees them)
        e["content"] = " ".join(r.choice(FRAG) for _ in range(r.randint(1, 12)))
    return evs


def _searches(seed):
    r = random.Random(seed)
    return [{"search": " ".join(r.sample(FRAG, r.randint(1, 2))), "limit": r.choice([5, 50, 500])} for _ in range(150)]


def test_a_copy_defers_words_and_searches_still_answer_exactly(tmp_path):
    evs = _with_text(5, 3000)
    worded = Store(str(tmp_path / "a"), flush_interval=3600, direct_durable=False)
    copied = Store(str(tmp_path / "b"), flush_interval=3600, direct_durable=False)
    try:
        for e in evs:
            worded.put(dict(e), origin="wot")
            copied.copy_put(dict(e), origin="wot")
        assert copied.words_pending == len(evs)
        for f in _searches(5):
            assert [e["id"] for e in copied.query(dict(f))] == [e["id"] for e in worded.query(dict(f))], f
        assert copied.words_pending == 0, "the first search did not catch the deferred words up"
    finally:
        worded.close()
        copied.close()


def test_a_replay_defers_words_and_answers_exactly(tmp_path):
    evs = _with_text(6, 2000)
    p = str(tmp_path / "db")
    st = Store(p, flush_interval=3600, direct_durable=False, snapshots=False)
    for e in evs:
        st.put(dict(e), origin="wot")
    want = [[e["id"] for e in st.query(dict(f))] for f in _searches(6)]
    st.close()
    again = Store(p, snapshots=False)
    try:
        assert again.words_pending == len(evs), "a replay tokenized every event again"
        assert [[e["id"] for e in again.query(dict(f))] for f in _searches(6)] == want
    finally:
        again.close()


def test_a_snapshot_is_only_taken_of_a_complete_index(tmp_path):
    p = str(tmp_path / "db")
    st = Store(p, flush_interval=3600, direct_durable=False, snapshot_min_events=1)
    for e in _with_text(7, 1500):
        st.copy_put(dict(e), origin="wot")
    assert st.snapshot() is None, "a snapshot was written with search words missing"
    st.close()                                         # nor at a stop
    import os
    assert not os.path.exists(os.path.join(p, snap.NAME))
    st = Store(p, flush_interval=3600, direct_durable=False, snapshot_min_events=1)
    st.index_pending_words()
    assert st.snapshot() is not None
    st.close()
    again = Store(p)
    try:
        assert again.last_open["snapshot"] and again.words_pending == 0
    finally:
        again.close()
