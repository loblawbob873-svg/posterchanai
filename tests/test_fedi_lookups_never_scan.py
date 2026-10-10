"""Fediverse lookups answer from secondary indexes, never by scanning the whole table.

2026-10-10: every inbox activity scanned the puppet registry (23k rows) to check a block, and -- until the ledger's
relay index is complete -- the 388k-row delivered-notes ledger to dedup a note. The app process spent a fifth of its
CPU there. The indexes follow every change (load, write, delete) through DocTable's watcher.
"""
import asyncio

import pytest

from app.services import fedi_tables, table_migrate as tm
from app.services.doc_table import DocTable
from tests.doc_table_mem import mem_tables


@pytest.fixture
def mem(monkeypatch):
    DocTable._registry.clear()
    m = mem_tables(monkeypatch)
    for i in range(500):
        m.put(fedi_tables.PUPPETS, "https://h%d.example/users/u%d" % (i % 7, i),
              {"actor_uri": "https://h%d.example/users/u%d" % (i % 7, i), "acct": "u%d@h%d.example" % (i, i % 7),
               "pubkey_hex": "%064x" % (i + 1)})
        m.put(fedi_tables.LEDGER, "activitypub|x|n%d" % i,
              {"platform": "activitypub", "note_uri": "https://h.example/n/%d" % i,
               "nostr_event_id": "%064x" % (i + 9000), "created_at": "2026-10-10T00:00:00"})
    asyncio.run(DocTable(fedi_tables.PUPPETS).aload())
    asyncio.run(DocTable(fedi_tables.LEDGER).aload())

    async def no_ready(**_kw):
        return False                              # the ledger's relay index is not built yet: the fallback path
    monkeypatch.setattr(fedi_tables, "_aindex_ready", no_ready)
    yield m
    DocTable._registry.clear()


def _forbid_scans(monkeypatch):
    def boom(*_a, **_kw):
        raise AssertionError("a lookup scanned the whole table")

    async def aboom(*_a, **_kw):
        boom()
    fedi_tables.puppet_pubkeys()                  # first use builds the index (one pass, then never again)
    asyncio.run(fedi_tables._arows_by_uri("warm"))
    monkeypatch.setattr(tm, "view", boom)
    monkeypatch.setattr(tm, "aview", aboom)


def test_puppet_and_ledger_lookups_use_the_index(mem, monkeypatch):
    _forbid_scans(monkeypatch)
    assert fedi_tables.puppet_pubkeys_for("https://h3.example/users/u3", "") == {"%064x" % 4}
    assert fedi_tables.puppet_pubkeys_for("", "u10@h3.example") == {"%064x" % 11}
    assert len(fedi_tables.puppet_pubkeys()) == 500
    row = asyncio.run(fedi_tables.adelivered_by_uri("https://h.example/n/42"))
    assert row.nostr_event_id == "%064x" % 9042
    assert asyncio.run(fedi_tables.amirrored_among(["%064x" % 9007, "f" * 64])) == {"%064x" % 9007}
    assert [r.acct for r in asyncio.run(fedi_tables.apuppets_with_acct("u5@h5.example"))] == ["u5@h5.example"]


def test_the_index_follows_writes_and_deletes(mem, monkeypatch):
    _forbid_scans(monkeypatch)
    led = DocTable(fedi_tables.LEDGER)
    asyncio.run(led.aput("activitypub|x|new", {"note_uri": "https://h.example/n/new", "nostr_event_id": "a" * 64}))
    assert asyncio.run(fedi_tables.adelivered_by_uri("https://h.example/n/new")).nostr_event_id == "a" * 64
    asyncio.run(led.adelete("activitypub|x|n42"))
    assert asyncio.run(fedi_tables.adelivered_by_uri("https://h.example/n/42")) is None
    pup = DocTable(fedi_tables.PUPPETS)
    asyncio.run(pup.aput("https://h3.example/users/u3",
                         {"actor_uri": "https://h3.example/users/u3", "acct": "renamed@h3.example",
                          "pubkey_hex": "b" * 64}))
    assert fedi_tables.puppet_pubkeys_for("", "u3@h3.example") == set(), "a changed handle kept its old entry"
    assert fedi_tables.puppet_pubkeys_for("", "renamed@h3.example") == {"b" * 64}
    assert "%064x" % 4 not in fedi_tables.puppet_pubkeys() and "b" * 64 in fedi_tables.puppet_pubkeys()
