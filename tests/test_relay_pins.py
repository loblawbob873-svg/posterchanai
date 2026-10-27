"""Pinned authors: listed with what each keeps, removable, optionally expiring -- and a MEMBER is never exposed.

Asked 2026-10-27, after measuring that 135 "Sync this user's data" pins kept ~520K events a 7-day retention would
otherwise age out: a list in Admin -> Relay with an Unpin, plus an optional expiry (off by default), and "with option
3, poster.place users will be protected right?". Yes, and this is the test of it: a pin is only ever ONE reason to
keep somebody; an account or NIP-05 holder here is preserved by the member set whether pinned or not.

Run against the relay's real store (PosterChanDB) and the shipped thread.py helpers, the real prune deciding.
"""
import asyncio
import json
import time

import pytest

from app.services.nostr_relay import thread as T
from tests.test_relay_prune import DAY, _ev, _run, store_factory  # noqa: F401  (fixture)

STRANGER, MEMBER, OTHER = "b" * 64, "c" * 64, "d" * 64


async def _pins(store, pins):
    await store.kv_set("pinned_pubkeys", " ".join(sorted(pins)))
    await store.kv_set("pinned_at", json.dumps(pins))


def test_the_list_says_how_much_each_pin_keeps(store_factory):
    async def go(loop):
        store = store_factory(loop, retention_days=7)
        await store.add_events_bulk([_ev(i, age_days=30, pubkey=STRANGER) for i in range(1, 21)]
                                    + [_ev(i, age_days=1, pubkey=STRANGER) for i in range(100, 103)]   # inside the window
                                    + [_ev(i, age_days=30, pubkey=OTHER) for i in range(200, 205)])
        return await store.aged_kept_by([STRANGER, OTHER, "e" * 64])

    assert _run(go) == {STRANGER: 20, OTHER: 5, "e" * 64: 0}


def test_unpin_frees_a_stranger_and_never_a_member(store_factory):
    async def go(loop):
        store = store_factory(loop, retention_days=7)
        await store.add_events_bulk([_ev(i, age_days=30, pubkey=STRANGER) for i in range(1, 11)]
                                    + [_ev(i, age_days=30, pubkey=MEMBER) for i in range(100, 110)])
        now = int(time.time())
        await _pins(store, {STRANGER: now, MEMBER: now})
        store.set_preserve_pubkeys([STRANGER, MEMBER])
        gone = await T._unpin(store, [STRANGER, MEMBER], members={MEMBER})
        pins = await T._pins_read(store)
        await store.prune()
        left = {e["pubkey"] for e in await store.query([{"kinds": [1]}])}
        n_member = len(await store.query([{"kinds": [1], "authors": [MEMBER]}]))
        return gone, pins, left, n_member, set(store.preserve_pubkeys)

    gone, pins, left, n_member, preserve = _run(go)
    assert sorted(gone) == sorted([STRANGER, MEMBER]) and pins == {}, (gone, pins)
    assert MEMBER in preserve and STRANGER not in preserve, preserve
    assert n_member == 10, "a MEMBER lost posts because their pin was removed"
    assert STRANGER not in left, "unpinning freed nothing: the stranger's old posts are still kept"


def test_an_unreadable_member_list_unpins_nobody():
    """The control command reads the members first; with none read it refuses (an empty set cannot tell a member
    from a stranger). Pinned here at the helper: an empty `members` would drop everyone from the preserve set."""
    class Store:
        def __init__(self):
            self.kv, self.preserve_pubkeys = {}, frozenset([MEMBER])
        async def kv_get(self, k): return self.kv.get(k)
        async def kv_set(self, k, v): self.kv[k] = v
        def set_preserve_pubkeys(self, p): self.preserve_pubkeys = frozenset(p)
    st = Store()

    async def go():
        await _pins(st, {MEMBER: 1})
        return await T._unpin(st, [MEMBER], members={MEMBER})
    asyncio.run(go())
    assert MEMBER in st.preserve_pubkeys


def test_pins_from_before_dates_are_dated_once(store_factory):
    async def go(loop):
        store = store_factory(loop, retention_days=7)
        await store.kv_set("pinned_pubkeys", f"{STRANGER} {MEMBER}")        # the old shape: no dates at all
        first = await T._pins_read(store)
        await asyncio.sleep(1.1)
        second = await T._pins_read(store)
        return first, second

    first, second = _run(go)
    assert set(first) == {STRANGER, MEMBER} and all(abs(t - time.time()) < 30 for t in first.values()), first
    assert first == second, "an old pin's date moved: an expiry would never reach it"


@pytest.mark.parametrize("days,members,expect_gone", [
    (0, {MEMBER}, False),         # expiry off (the default): nothing expires, however old
    (30, {MEMBER}, True),         # on, and the pin is older than that: it goes
    (30, set(), False),           # on, but the member list could not be read: nothing expires this pass
])
def test_old_pins_expire_only_when_switched_on(store_factory, monkeypatch, days, members, expect_gone):
    monkeypatch.setattr(T, "_pin_days", lambda: days)
    monkeypatch.setattr(T, "_members_now", lambda: set(members))

    async def go(loop):
        store = store_factory(loop, retention_days=7)
        old = int(time.time()) - 90 * DAY
        await _pins(store, {STRANGER: old, MEMBER: old})
        store.set_preserve_pubkeys([STRANGER, MEMBER])
        await T._refresh_preserve(store)
        return await T._pins_read(store), set(store.preserve_pubkeys)

    pins, preserve = _run(go)
    if expect_gone:
        assert pins == {}, pins
        assert STRANGER not in preserve and MEMBER in preserve, preserve
    else:
        assert set(pins) == {STRANGER, MEMBER}, pins
        assert {STRANGER, MEMBER} <= preserve, preserve
