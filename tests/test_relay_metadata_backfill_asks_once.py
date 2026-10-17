"""The relay's metadata backfill asks for a missing profile once per 6 hours, not every 45 seconds.

Measured on server1 2026-10-16: every 45 s it re-scanned the whole store for members without a profile and asked
every upstream relay (new TLS connections each time) for the same ~1,500 pubkeys -- most have no profile anywhere --
~14% of the relay's CPU.
"""
import asyncio

import pytest

from app.services.nostr_relay import ingest


class Store:
    def __init__(self, missing):
        self.missing, self.scans = missing, 0

    async def wot_missing_metadata(self):
        self.scans += 1
        return list(self.missing)

    async def add_event(self, ev, origin=""):
        return True


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    ingest._asked.clear()
    ingest._missing_memo.update(at=float("-inf"), list=[])
    asked = []

    async def query(upstream, filters, direct=False):
        asked.extend(filters[0]["authors"])
        return []
    monkeypatch.setattr(ingest._relay, "query", query)
    return asked


def _run(store):
    return asyncio.run(ingest.fetch_lookup_metadata(store, ["wss://up.example"], 100, 1500, pace=0))


def test_a_pass_every_45_seconds_asks_each_missing_profile_once(fresh, monkeypatch):
    store = Store(["%064x" % i for i in range(300)])
    for _ in range(10):                                      # 7.5 minutes of passes
        _run(store)
    assert len(fresh) == 300 and len(set(fresh)) == 300, ("the same pubkeys were asked again", len(fresh))
    assert store.scans == 1, ("the whole store was re-scanned every pass", store.scans)


def test_new_members_and_old_ones_are_asked_again_later(fresh, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(ingest.time, "monotonic", lambda: t[0])
    store = Store(["a" * 64])
    _run(store)
    store.missing.append("b" * 64)
    t[0] += ingest._MISSING_EVERY + 1                        # the list is recomputed: the newcomer is asked
    _run(store)
    assert fresh == ["a" * 64, "b" * 64]
    t[0] += ingest._ASK_AGAIN                                # hours later both are worth asking again
    _run(store)
    assert sorted(fresh[2:]) == ["a" * 64, "b" * 64]
