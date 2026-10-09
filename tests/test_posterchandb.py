"""PosterChanDB: the codec is lossless, the store keeps NIP-01's rules, and nothing written is lost or misread.

"test it very good first before switching over". These run the shipped store on generated events and on
a temp directory; tests/test_posterchandb_parity.py compares it against Postgres on real data.
"""
import hashlib
import json
import os
import random
import string

import pytest

from app.services.posterchandb.codec import Codec
from app.services.posterchandb.store import Store

R = random.Random(1009)


def hx(n=64):
    return "".join(R.choice("0123456789abcdef") for _ in range(n))


def ev_id(ev):
    return hashlib.sha256(json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                                     separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def mk(kind=1, content="hello world", tags=None, pubkey=None, created_at=None):
    ev = {"pubkey": pubkey or hx(), "created_at": created_at if created_at is not None else R.randint(1_600_000_000, 1_800_000_000),
          "kind": kind, "tags": tags if tags is not None else [], "content": content, "sig": hx(128)}
    ev["id"] = ev_id(ev)
    return ev


# ---------------------------------------------------------------- codec
CONTENTS = [
    "", "gm", "hello world " * 50, "ünïcødé ✨ 日本語 🧅",
    "A" * 44 + "==",                                            # base64-looking
    "dGhpcyBpcyBhIG5pcDA0IG1lc3NhZ2UgZm9yIHRlc3Q=?iv=MDEyMzQ1Njc4OWFiY2RlZg==",   # NIP-04
    "AhJ7" + "".join(R.choice(string.ascii_letters + string.digits + "+/") for _ in range(400)),  # NIP-44-like (len%4==0)
    "Zm9v", "Zm9v\n", "not=base64?iv=x", "  leading and trailing  ", "line1\r\nline2\n", "\x00nul inside",
]


@pytest.mark.parametrize("content", CONTENTS)
def test_every_content_shape_round_trips_and_still_hashes_to_its_id(content):
    c = Codec(b"hello world the a and to " * 100)
    ev = mk(content=content, tags=[["p", hx()], ["e", hx(), "wss://r.example", "reply"], ["P", hx().upper()],
                                   ["t", "nostr"], ["d", ""], ["x"], ["imeta", "url https://x", "m image/png"]])
    back = c.decode(c.encode(ev))
    assert back == ev
    assert ev_id(back) == ev["id"]


def test_uppercase_or_short_hex_in_a_tag_is_kept_as_written():
    c = Codec()
    ev = mk(tags=[["p", hx().upper()], ["e", hx()[:63]], ["e", hx() + "0"]])
    assert c.decode(c.encode(ev)) == ev


def test_invalid_events_are_refused_not_mangled():
    c = Codec()
    for bad in ({"id": "ABC"}, {"sig": "zz"}, {"created_at": 1.5}, {"kind": -1}, {"tags": [["p", float("nan")]]},
                {"content": None}):
        ev = mk()
        ev.update(bad)
        with pytest.raises((ValueError, KeyError, TypeError)):
            c.encode(ev)


def test_non_string_tag_elements_real_relays_store_come_back_exactly():
    """Found loading poster.place's relay: events whose tags hold numbers/null/lists. Invalid per NIP-01, but
    stored, and their id hashes over them — so they must round-trip value-for-value AND type-for-type."""
    c = Codec()
    ev = mk(tags=[["amount", 21000], ["x", 1.5], ["y", None], ["z", True], ["w", ["a", 1]], ["p", hx()]])
    back = c.decode(c.encode(ev))
    assert back == ev and [type(v) for t in back["tags"] for v in t] == [type(v) for t in ev["tags"] for v in t]
    assert ev_id(back) == ev["id"]


def test_non_string_tags_are_stored_and_indexed_the_way_the_relay_indexes_them(tmp_path):
    s = Store(str(tmp_path / "db"))
    ev = mk(kind=1, created_at=1_700_000_000, tags=[[7, "x"], ["t", 21000], ["e", None], ["amount", 5]])
    assert s.put(ev) == "stored"
    assert [e["id"] for e in s.query({"#t": ["21000"]}, now=1_800_000_000)] == [ev["id"]]   # str(21000)
    assert [e["id"] for e in s.query({"#e": ["None"]}, now=1_800_000_000)] == [ev["id"]]
    s.close()
    s = Store(str(tmp_path / "db"))
    assert s.get(s.seq_of(ev["id"])) == ev
    s.close()


def test_compact_encoding_is_smaller_than_json_on_typical_events():
    c = Codec((" the a to and of in is it you that " * 300).encode())
    evs = [mk(content="the quick brown fox and the lazy dog in the park " * 3,
              tags=[["p", hx()] for _ in range(5)] + [["e", hx(), "", "root"]]) for _ in range(200)]
    js = sum(len(json.dumps(e, separators=(",", ":")).encode()) for e in evs)
    cb = sum(len(c.encode(e)) for e in evs)
    assert cb < js * 0.7, (cb, js)


# ---------------------------------------------------------------- store: NIP-01 semantics
@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "db"), flush_interval=60)
    yield s
    s.close()


def ids(evs):
    return [e["id"] for e in evs]


def test_newest_first_with_limit_since_until(store):
    pk = hx()
    evs = [mk(pubkey=pk, created_at=1000 + i) for i in range(20)]
    for e in evs:
        assert store.put(e) == "stored"
    got = store.query({"authors": [pk], "kinds": [1], "limit": 5})
    assert ids(got) == ids(sorted(evs, key=lambda e: -e["created_at"])[:5])
    got = store.query({"authors": [pk], "since": 1005, "until": 1007})
    assert sorted(e["created_at"] for e in got) == [1005, 1006, 1007]
    assert store.put(evs[0]) == "duplicate"


def test_tag_filters_and_or_semantics(store):
    a, b = hx(), hx()
    e1 = mk(tags=[["p", a]]); e2 = mk(tags=[["p", b]]); e3 = mk(tags=[["p", a], ["t", "x"]])
    for e in (e1, e2, e3):
        store.put(e)
    assert set(ids(store.query({"#p": [a]}))) == {e1["id"], e3["id"]}
    assert set(ids(store.query({"#p": [a, b]}))) == {e1["id"], e2["id"], e3["id"]}
    assert ids(store.query({"#p": [a], "#t": ["x"]})) == [e3["id"]]


def test_replaceable_newest_wins_and_ties_go_to_the_lower_id(store):
    pk = hx()
    old = mk(kind=0, pubkey=pk, created_at=100, content="old")
    new = mk(kind=0, pubkey=pk, created_at=200, content="new")
    assert store.put(new) == "stored"
    assert store.put(old) == "superseded"
    assert ids(store.query({"authors": [pk], "kinds": [0]})) == [new["id"]]
    t1 = mk(kind=3, pubkey=pk, created_at=300, content="a")
    t2 = mk(kind=3, pubkey=pk, created_at=300, content="b")
    lo, hi = sorted([t1, t2], key=lambda e: e["id"])
    store.put(hi)
    store.put(lo)
    assert ids(store.query({"authors": [pk], "kinds": [3]})) == [lo["id"]]


def test_addressable_events_are_per_d_tag(store):
    pk = hx()
    a1 = mk(kind=30078, pubkey=pk, created_at=10, tags=[["d", "pcai:note:1"]])
    a2 = mk(kind=30078, pubkey=pk, created_at=20, tags=[["d", "pcai:note:1"]])
    b1 = mk(kind=30078, pubkey=pk, created_at=5, tags=[["d", "pcai:note:2"]])
    for e in (a1, a2, b1):
        store.put(e)
    got = store.query({"authors": [pk], "kinds": [30078]})
    assert set(ids(got)) == {a2["id"], b1["id"]}
    assert ids(store.query({"#d": ["pcai:note:1"]})) == [a2["id"]]


def test_deletion_only_by_the_author(store):
    pk, other = hx(), hx()
    mine = mk(pubkey=pk); theirs = mk(pubkey=other)
    store.put(mine); store.put(theirs)
    store.put(mk(kind=5, pubkey=pk, tags=[["e", mine["id"]], ["e", theirs["id"]]]))
    assert store.query({"ids": [mine["id"]]}) == []
    assert ids(store.query({"ids": [theirs["id"]]})) == [theirs["id"]]
    addr = mk(kind=30023, pubkey=pk, created_at=100, tags=[["d", "post"]])
    store.put(addr)
    store.put(mk(kind=5, pubkey=pk, created_at=150, tags=[["a", "30023:%s:post" % pk]]))
    assert store.query({"authors": [pk], "kinds": [30023]}) == []


def test_expired_and_ephemeral_are_never_returned(store):
    e = mk(tags=[["expiration", "1000"]])
    store.put(e)
    assert store.query({"ids": [e["id"]]}, now=999) and store.query({"ids": [e["id"]]}, now=1001) == []
    assert store.put(mk(kind=20001)) == "ephemeral"


def test_search_matches_every_word_ignores_case_and_follows_postgres_tokens(store):
    a = mk(content="The Ferry leaves at seven https://ferries.example/timetable.pdf")
    b = mk(content="ferry tickets")
    c = mk(content="A" * 60 + "==")
    for e in (a, b, c):
        store.put(e)
    assert set(ids(store.query({"search": "ferry"}))) == {a["id"], b["id"]}
    assert ids(store.query({"search": "FERRY seven"})) == [a["id"]]
    # Postgres indexes base64 too (its parser sees an asciiword) — the relay finds it, so must we
    assert ids(store.query({"search": "A" * 60})) == [c["id"]]
    # …and a word inside a URL is NOT a word to Postgres: the URL, its host and its path are the tokens
    assert store.query({"search": "timetable"}) == []
    assert ids(store.query({"search": "ferries.example"})) == [a["id"]]


# ---------------------------------------------------------------- durability
def test_a_clean_close_loses_nothing_and_reopen_gives_the_same_answers(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600)
    evs = [mk(pubkey="a" * 64, created_at=1000 + i, tags=[["t", "x"]]) for i in range(300)]
    for e in evs:
        s.put(e)
    before = ids(s.query({"authors": ["a" * 64], "limit": 1000}))
    s.close()                                         # a clean stop flushes
    s2 = Store(p)
    assert ids(s2.query({"authors": ["a" * 64], "limit": 1000})) == before
    assert len(s2.query({"#t": ["x"], "limit": 1000})) == 300
    s2.close()


def test_delayed_writes_are_not_on_disk_until_the_flush_but_direct_writes_are(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=60, direct_durable=True)
    size0 = os.path.getsize(os.path.join(p, "seg-000001.log"))
    s.put(mk())                                       # copied from elsewhere: waits for the timer
    assert os.path.getsize(os.path.join(p, "seg-000001.log")) == size0
    assert s.maybe_flush() == 0                       # the 60 s have not passed
    s.put(mk(), direct=True)                          # written here: on disk before put() returns
    assert os.path.getsize(os.path.join(p, "seg-000001.log")) > size0
    assert s.stats()["unflushed"] == 0
    s.close()


def test_a_torn_or_corrupt_tail_is_cut_off_and_everything_before_it_survives(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p)
    evs = [mk() for _ in range(50)]
    for e in evs:
        s.put(e)
    s.close()
    log = os.path.join(p, "seg-000001.log")
    good = os.path.getsize(log)
    with open(log, "ab") as f:                        # a write cut short by power loss
        f.write(b"\x40\x00\x00\x00\xde\xad\xbe\xefpartial")
    notes = []
    s2 = Store(p, log=notes.append)
    assert os.path.getsize(log) == good and notes, "the torn tail was not cut off"
    assert len(s2.query({"limit": 1000})) == 50
    s2.put(mk())
    s2.close()
    # a flipped byte inside the LAST record: that record is dropped, the rest survive
    with open(log, "r+b") as f:
        f.seek(-5, os.SEEK_END)
        b = f.read(1)
        f.seek(-5, os.SEEK_END)
        f.write(bytes([b[0] ^ 0xFF]))
    s3 = Store(p, log=lambda *a: None)
    assert len(s3.query({"limit": 1000})) == 50
    s3.close()


# ---------------------------------------------------------------- CPU: work per flush follows what arrived
def test_runs_stay_logarithmic_over_many_flushes(tmp_path):
    s = Store(str(tmp_path / "db"), flush_interval=3600)
    for batch in range(60):
        for _ in range(50):
            s.put(mk(tags=[["p", hx()]]))
        s.flush()
    n = len(s.off)
    assert len(s.idx.runs) <= 2 + int(__import__("math").log(n, 4)), (len(s.idx.runs), n)
    # and the answers are still complete
    assert len(s.query({"kinds": [1], "limit": 10000})) == n
    s.close()


# ---------------------------------------------------------------- the relay's own query rules (store.py _query_one/_build_where)
def test_equal_timestamps_come_back_higher_id_first(store):
    evs = [mk(created_at=5000) for _ in range(6)]
    for e in evs:
        store.put(e)
    got = store.query({"kinds": [1]})
    assert ids(got) == sorted(ids(evs), reverse=True)


def test_limit_is_or_500_clamped_to_1_and_5000(store):
    for i in range(520):
        store.put(mk(created_at=10_000 + i))
    assert len(store.query({"kinds": [1]})) == 500
    assert len(store.query({"kinds": [1], "limit": 0})) == 500           # `limit or 500`
    assert len(store.query({"kinds": [1], "limit": 3})) == 3
    assert len(store.query({"kinds": [1], "limit": 99999})) == 520       # capped at 5000, only 520 exist


def test_limit_never_drops_an_event_at_the_cut_off_timestamp_for_a_later_one(store):
    evs = [mk(created_at=100) for _ in range(5)] + [mk(created_at=50) for _ in range(5)]
    for e in evs:
        store.put(e)
    got = store.query({"kinds": [1], "limit": 3})
    assert ids(got) == sorted([e["id"] for e in evs if e["created_at"] == 100], reverse=True)[:3]


def test_cursor_pages_through_everything_exactly_once(store):
    pk = hx()
    evs = [mk(kind=30078, pubkey=pk, created_at=7000 + (i // 4), tags=[["d", "doc:%d" % i]]) for i in range(40)]
    for e in evs:
        store.put(e)
    seen, cursor = [], None
    while True:
        flt = {"authors": [pk], "kinds": [30078], "limit": 7}
        if cursor:
            flt["_cursor"] = cursor
        page = store.query(flt)
        if not page:
            break
        seen += ids(page)
        cursor = [page[-1]["created_at"], page[-1]["id"]]
    assert sorted(seen) == sorted(ids(evs)) and len(seen) == len(set(seen)) == 40


def test_d_prefix_is_a_prefix_and_percent_underscore_are_literal(store):
    pk = hx()
    inbox = [mk(kind=30078, pubkey=pk, tags=[["d", "pcai:mail:me@x:INBOX:%d" % i]]) for i in range(3)]
    other = mk(kind=30078, pubkey=pk, tags=[["d", "pcai:mail:me@x:Sent:1"]])
    pct = mk(kind=30078, pubkey=pk, tags=[["d", "a%b_c:1"]])
    near = mk(kind=30078, pubkey=pk, tags=[["d", "aXbYc:1"]])
    for e in inbox + [other, pct, near]:
        store.put(e)
    assert set(ids(store.query({"#d~": ["pcai:mail:me@x:INBOX:"]}))) == set(ids(inbox))
    assert ids(store.query({"#d~": ["a%b_"]})) == [pct["id"]]          # never a LIKE wildcard
    assert store.query({"#d~": ["pcai:mail:nobody:"]}) == []


def test_quote_authors_count_as_mentions_only_when_asked(store):
    author = hx()
    quote = mk(tags=[["q", hx(), "", author]])
    store.put(quote)
    store.add_derived_tag(quote["id"], "_quote_author", author)
    assert store.query({"#p": [author]}) == []
    assert ids(store.query({"#p": [author], "_include_quotes": True})) == [quote["id"]]


def test_empty_filter_lists_are_ignored_like_the_relay_does(store):
    e = mk()
    store.put(e)
    assert ids(store.query({"ids": [], "authors": [], "kinds": [], "#p": []})) == [e["id"]]


def test_derived_tags_and_deletions_survive_a_restart(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p)
    author, pk = hx(), hx()
    quote = mk(tags=[["q", hx()]])
    gone = mk(pubkey=pk)
    s.put(quote); s.put(gone)
    s.add_derived_tag(quote["id"], "_quote_author", author)
    s.put(mk(kind=5, pubkey=pk, tags=[["e", gone["id"]]]))
    s.close()
    s2 = Store(p)
    assert ids(s2.query({"#p": [author], "_include_quotes": True})) == [quote["id"]]
    assert s2.query({"ids": [gone["id"]]}) == []
    s2.close()
