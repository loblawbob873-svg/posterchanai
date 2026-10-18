"""A restart loads each table from its own snapshot plus what changed since -- never a full re-read when it can vouch.

2026-10-18: every restart re-read every table in full (the 190k-row blob index, the 165k-row fediverse ledger ...),
~380,000 documents through the relay per process per deploy -- the relay's largest burst ("you better fucking fix
that"). Runs against a real relay: rows written, changed and DELETED by another process while this one was down must
all be right after the restart, and anything doubtful must fall back to the full load.
"""
import asyncio
import os

import pytest

from app.services import doc_table, nostr_store
from tests.test_doc_table import _fresh, _until, relay  # noqa: F401  (fixture)


@pytest.fixture
def snapdir(tmp_path, monkeypatch):
    d = tmp_path / "snaps"
    monkeypatch.setenv("PC_DOCTABLE_SNAPSHOT_DIR", str(d))
    return d


def _truth(name, monkeypatch):
    monkeypatch.setenv("PC_DOCTABLE_SNAPSHOT_DIR", "")
    t = _fresh(name)
    rows = t.all()
    assert t.loaded_from == "relay"
    return rows


def _stop(t):
    assert _until(lambda: t._live_ok), "the live stream never caught up"
    assert t.save_snapshot(), "a loaded table with a current stream did not save its snapshot"


def test_a_restart_is_its_snapshot_plus_what_changed_and_never_a_full_read(relay, snapdir, monkeypatch):  # noqa: F811
    t = doc_table.DocTable("snapx")
    for i in range(60):
        t.put(str(i), {"n": i, "owner": "a" * 64})
    assert len(t.all()) == 60
    _stop(t)                                                 # a clean stop
    theirs = _fresh("snapx")                                 # ANOTHER process, while this one is down
    asyncio.run(theirs.aput("5", {"n": 500}))
    asyncio.run(theirs.aput("new", {"n": -1}))
    asyncio.run(theirs.adelete("7"))
    full = []
    real = nostr_store.list_all_docs

    async def counting(*a, **k):
        full.append(1)
        return await real(*a, **k)
    monkeypatch.setattr(nostr_store, "list_all_docs", counting)

    t2 = _fresh("snapx")                                     # the restart
    rows = t2.all()
    assert t2.loaded_from == "snapshot" and full == [], "the restart re-read the whole table"
    monkeypatch.setattr(nostr_store, "list_all_docs", real)
    assert rows == _truth("snapx", monkeypatch), "the snapshot + delta is not the table"
    assert rows["5"] == {"n": 500} and rows["new"] == {"n": -1} and "7" not in rows


def test_a_tampered_or_expired_snapshot_is_the_full_load(relay, snapdir, monkeypatch):  # noqa: F811
    t = doc_table.DocTable("snapy")
    for i in range(10):
        t.put(str(i), {"n": i})
    t.all()
    _stop(t)
    path = os.path.join(str(snapdir), "snapy.snap")
    blob = bytearray(open(path, "rb").read())
    blob[-5] ^= 1
    open(path, "wb").write(bytes(blob))
    t2 = _fresh("snapy")
    assert t2.all() == {str(i): {"n": i} for i in range(10)} and t2.loaded_from == "relay"

    _stop(t2)                                                # a good one again, then too old
    monkeypatch.setattr(doc_table, "_SNAP_MAX_AGE", -1)
    t3 = _fresh("snapy")
    assert t3.all() == {str(i): {"n": i} for i in range(10)} and t3.loaded_from == "relay"


def test_no_snapshot_is_written_for_a_table_whose_stream_is_not_current(relay, snapdir):  # noqa: F811
    t = doc_table.DocTable("snapz")
    t.put("1", {"n": 1})
    t.all()
    t._live_ok = False                                       # the stream dropped: rows may be behind
    assert not t.save_snapshot()
    assert not os.path.exists(os.path.join(str(snapdir), "snapz.snap"))
