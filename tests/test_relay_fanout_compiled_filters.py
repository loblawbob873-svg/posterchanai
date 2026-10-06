"""Live fan-out matches with filters compiled ONCE per subscription -- and answers exactly as before.

py-spy on server1's relay (2026-10-06): matching each stored event against every open subscription was
~10% of the relay's CPU. `authors` (hundreds of keys on a follow feed) was scanned as a list, and kinds /
tag-value sets were rebuilt for every event. SubscriptionManager.add now compiles them; this file proves
the compiled form gives the SAME verdict as the raw one over thousands of generated filter/event pairs,
including the junk a client can send.
"""
import random
import time

from app.services.nostr_relay import server as S

KEYS = [f"{i:064x}" for i in range(40)]


def _event(r):
    tags = [["p", r.choice(KEYS)] for _ in range(r.randint(0, 3))] + \
           [["e", r.choice(KEYS)] for _ in range(r.randint(0, 2))] + \
           ([["t", r.choice(["nostr", "art", "Nostr"])]] if r.random() < .5 else [])
    return {"id": r.choice(KEYS), "pubkey": r.choice(KEYS), "kind": r.choice([0, 1, 6, 7, 1111, 30023]),
            "created_at": r.randint(1000, 2000), "tags": tags, "content": r.choice(["hello world", "Art post"])}


def _filter(r):
    f = {}
    if r.random() < .4: f["ids"] = r.sample(KEYS, r.randint(1, 5))
    if r.random() < .5: f["authors"] = r.sample(KEYS, r.randint(1, 20))
    if r.random() < .5: f["kinds"] = r.choice([[1], [1, 6], ["1", 7], [0, 30023]])
    if r.random() < .3: f["since"] = r.randint(900, 1600)
    if r.random() < .3: f["until"] = r.randint(1400, 2100)
    if r.random() < .3: f["#p"] = r.sample(KEYS, r.randint(1, 4))
    if r.random() < .2: f["#e"] = r.sample(KEYS, 2)
    if r.random() < .2: f["#t"] = r.choice([["nostr"], ["art", "nostr"], [1]])
    if r.random() < .1: f["search"] = r.choice(["hello", "art"])
    if r.random() < .05: f["#p"] = []                      # an empty tag list is ignored
    if r.random() < .05: f["kinds"] = ["x"]                # junk that cannot compile
    return f


def _verdict(flt, ev, **kw):
    try:
        return S._match_one(flt, ev, **kw)
    except Exception as e:                                # the same error, compiled or not
        return type(e).__name__


def test_a_compiled_filter_answers_exactly_like_the_raw_one():
    r = random.Random(20261006)
    for _ in range(6000):
        flt, ev = _filter(r), _event(r)
        assert _verdict(S._compile_filter(flt), ev, _tags={}) == _verdict(flt, ev), (flt, ev)


def test_quoted_pubkeys_do_not_leak_between_filters_through_the_tag_cache(monkeypatch):
    """`#p` with _include_quotes adds the quote's author -- to THAT check only, never into the cache."""
    monkeypatch.setattr(S, "quote_pubkeys", lambda ev: {KEYS[9]})
    ev = {"id": KEYS[0], "pubkey": KEYS[1], "kind": 1, "created_at": 1500, "tags": [["p", KEYS[2]]]}
    cache = {}
    assert S._match_one(S._compile_filter({"#p": [KEYS[9]], "_include_quotes": True}), ev, cache)
    assert not S._match_one(S._compile_filter({"#p": [KEYS[9]]}), ev, cache), "the quote leaked into the cache"


def test_fanout_delivers_to_exactly_the_matching_subscriptions():
    r = random.Random(7)
    m = S.SubscriptionManager()
    raw = {}
    for c in range(30):
        for s in range(3):
            fl = [_filter(r) for _ in range(r.randint(1, 2))]
            fl = [f for f in fl if f.get("kinds") != ["x"]]
            m.add(c, f"s{s}", fl)
            raw[(c, f"s{s}")] = fl
    for _ in range(300):
        ev = _event(r)
        got = []
        m.fanout(ev, lambda conn, msg: got.append((conn, msg[1])), lambda conn, ev: True)
        want = [k for k, fl in raw.items() if S._matches(fl, ev)]
        assert sorted(got) == sorted(want)


def test_it_is_faster_for_a_follow_feed():
    authors = [f"{i:064x}" for i in range(800)]
    flt = {"authors": authors, "kinds": [1, 6, 7]}
    ev = {"id": "a" * 64, "pubkey": "f" * 64, "kind": 1, "created_at": 1, "tags": []}
    comp = S._compile_filter(flt)
    t = time.perf_counter()
    for _ in range(2000): S._match_one(flt, ev)
    raw = time.perf_counter() - t
    t = time.perf_counter()
    for _ in range(2000): S._match_one(comp, ev, {})
    fast = time.perf_counter() - t
    assert fast * 5 < raw, (raw, fast)


def test_the_relay_compresses_at_level_3_with_the_library_defaults_otherwise():
    from app.services.nostr_relay import thread
    f = thread._relay_deflate()
    assert f.compress_settings == {"memLevel": 5, "level": 3}
    assert f.server_max_window_bits == 12 and f.client_max_window_bits == 12
