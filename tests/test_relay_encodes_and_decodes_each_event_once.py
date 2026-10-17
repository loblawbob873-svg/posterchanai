"""The relay decodes and encodes a popular event once, not once per answer and per subscriber.

py-spy on server1 (2026-10-16, ~1.1 cores steady): decoding stored events for query answers (~11%) and encoding +
compressing outgoing EVENT frames (~27%) were most of the relay's CPU -- the same contact lists, profiles and feed tops
again and again. Both are now kept once, bounded; these pin that the answers are byte-for-byte unchanged.
"""
import json
import os
import sys

from app.services.nostr_relay import server as S
from app.services.posterchandb.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ev(i):
    return {"id": "%064x" % i, "pubkey": "b" * 64, "created_at": 1000 + i, "kind": 1,
            "tags": [["p", "c" * 64]], "content": 'hé "q" \n %d' % i, "sig": "d" * 128}


def test_event_frames_are_identical_and_encoded_once(monkeypatch):
    calls = []
    real = json.dumps
    monkeypatch.setattr(S.json, "dumps", lambda o, *a, **k: (calls.append(o), real(o, *a, **k))[1])
    ev = _ev(1)
    frames = [S._frame(["EVENT", "sub%d" % n, ev]) for n in range(50)]
    assert all(f == real(["EVENT", "sub%d" % n, ev]) for n, f in enumerate(frames))
    assert sum(1 for c in calls if c is ev) == 1, "the event was encoded more than once for 50 subscribers"
    for o in (["OK", "x", True, ""], ["EOSE", "s"], ["NOTICE", "n"]):
        assert S._frame(o) == real(o)


def test_the_frame_cache_stays_bounded(monkeypatch):
    monkeypatch.setattr(S, "_EV_JSON_MAX", 100)
    for i in range(1000):
        S._frame(["EVENT", "s", _ev(10_000 + i)])
    assert len(S._EV_JSON) <= 100 and S._ev_json_bytes == sum(len(v) for v in S._EV_JSON.values())


def _events(n):
    sys.path.insert(0, ROOT)
    from tests.test_posterchandb_disk_full import _events as ev
    return ev(5, n)


def test_query_answers_decode_once_and_stay_independent(tmp_path):
    st = Store(str(tmp_path / "s"), flush_interval=3600, snapshots=False)
    try:
        evs = _events(200)
        for e in evs:
            st.put(e)
        decoded = []
        real = st.codec.decode
        st.codec.decode = lambda rec: (decoded.append(1), real(rec))[1]
        f = {"kinds": [1, 6, 7], "limit": 50}
        a = st.query(f)
        first = len(decoded)
        b = st.query(f)
        assert first and len(decoded) == first, "a repeated answer decoded its events again"
        assert [e["id"] for e in a] == [e["id"] for e in b]
        a[0]["content"] = "changed by one caller"
        assert st.query(f)[0]["content"] != "changed by one caller", "one caller's change reached the next answer"
        st.put({"id": "e" * 64, "pubkey": b[0]["pubkey"], "created_at": b[0]["created_at"] + 1, "kind": 5,
                "tags": [["e", b[0]["id"]]], "content": "", "sig": "0" * 128})      # the author deletes it
        assert b[0]["id"] not in [e["id"] for e in st.query(f)], "a deleted event came back from the cache"
    finally:
        st.close(take_snapshot=False)
