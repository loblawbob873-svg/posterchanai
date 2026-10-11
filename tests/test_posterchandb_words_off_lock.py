"""The search-word backlog must not stall the relay, and ciphertext is not tokenised.

2026-10-10, server1 after promotion: "1 CPU is constantly pegged for the relay". Two thirds of the relay's busy time
was the deferred search-word pass (~100 events/s), and it tokenised WHILE HOLDING THE STORE LOCK, so every query
waited behind each batch. Most of the characters it chewed through were NIP-44 ciphertext (encrypted app documents,
DMs, gift wraps): a quarter of the events, tens of KB each, and not one searchable word in any of it.
"""
import base64
import os
import threading
import time

from app.services.posterchandb import store as S
from app.services.posterchandb.store import Store
from tests.test_posterchandb_durability import _events


def _copied(tmp_path, n=40, seed=11):
    st = Store(str(tmp_path / "db"), flush_interval=3600, direct_durable=False)
    evs = _events(seed, n)
    for i, e in enumerate(evs):
        e["content"] = "hello word%d shared" % i
        st.copy_put(dict(e), origin="wot")
    return st, evs


def test_a_query_answers_while_a_batch_is_being_tokenised(tmp_path, monkeypatch):
    st, evs = _copied(tmp_path)
    inside, release = threading.Event(), threading.Event()
    real = S.tsparser.search_set

    def slow(text):
        inside.set()
        release.wait(10)
        return real(text)
    monkeypatch.setattr(S.tsparser, "search_set", slow)
    t = threading.Thread(target=st.index_pending_words, args=(25,))
    t.start()
    try:
        assert inside.wait(5)
        got = {}
        q = threading.Thread(target=lambda: got.setdefault("r", st.query({"kinds": [evs[0]["kind"]], "limit": 5})))
        q.start()
        q.join(2)
        assert not q.is_alive(), "a plain query waited for the word indexer's tokenising"
        assert got["r"]
    finally:
        release.set()
        t.join(10)
        st.close()


def test_a_search_during_a_batch_is_exact_and_nothing_is_indexed_twice(tmp_path, monkeypatch):
    st, evs = _copied(tmp_path)
    inside, release = threading.Event(), threading.Event()
    real = S.tsparser.search_set
    first = {"done": False}

    def slow(text):
        if not first["done"]:
            first["done"] = True
            inside.set()
            release.wait(10)
        return real(text)
    monkeypatch.setattr(S.tsparser, "search_set", slow)
    t = threading.Thread(target=st.index_pending_words, args=(25,))
    t.start()
    try:
        assert inside.wait(5)
        hits = st.query({"search": "word3", "limit": 50})          # catches everything up inline, exactly
        assert [e["id"] for e in hits] == [evs[3]["id"]]
        assert len(st.query({"search": "shared", "limit": 500})) == len(evs)
    finally:
        release.set()
        t.join(10)
    try:
        assert st.words_pending == 0
        assert len(st.query({"search": "shared", "limit": 500})) == len(evs), "a batch was indexed twice"
        assert [e["id"] for e in st.query({"search": "word3"})] == [evs[3]["id"]]
    finally:
        st.close()


def test_ciphertext_is_never_tokenised_and_text_still_is(tmp_path, monkeypatch):
    calls = []
    real = S.tsparser.search_set
    monkeypatch.setattr(S.tsparser, "search_set", lambda t: calls.append(len(t)) or real(t))
    st = Store(str(tmp_path / "db"), flush_interval=3600, direct_durable=False)
    try:
        evs = _events(12, 3)
        evs[0]["content"] = base64.b64encode(os.urandom(6000)).decode()                    # NIP-44 payload
        evs[1]["content"] = base64.b64encode(os.urandom(3000)).decode() + "?iv=" + "A" * 24  # NIP-04 payload
        evs[2]["content"] = "an ordinary post about relays"
        for e in evs:
            st.put(dict(e), origin="wot")
        assert all(n < 256 for n in calls), "ciphertext went through the tokenizer: %s" % calls
        assert [e["id"] for e in st.query({"search": "relays"})] == [evs[2]["id"]]
        t0 = time.perf_counter()
        for _ in range(200):
            S.index_words(evs[0]["content"])
        assert time.perf_counter() - t0 < 0.5
    finally:
        st.close()
