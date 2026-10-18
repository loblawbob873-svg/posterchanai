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


CHILD = r"""
import asyncio, os, signal, sys, time
sys.path.insert(0, os.getcwd())
os.environ["PC_DOCTABLE_SNAPSHOT_DIR"] = sys.argv[1] + "/snaps"
os.environ["POSTERCHANAI_PORT"] = "39999"            # not the main instance: shutdown stops nothing else
from tests.test_doc_table import _Relay, SK
from app.services import keystore, doc_table
from app.services.nostr import bech32
keystore.get_operator_nsec = lambda: bech32.encode("nsec", SK)
r = _Relay(sys.argv[1] + "/relay"); os.environ["POSTERCHANAI_RELAY_PORT"] = str(r.port)
t = doc_table.DocTable("snapexit"); t.put("1", {"n": 1}); t.all()
for _ in range(100):
    if t._live_ok: break
    time.sleep(0.1)
import app.main
asyncio.run(app.main.shutdown())
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.raise_signal(signal.SIGTERM)                 # what uvicorn does after its shutdown: no atexit
"""


def test_the_app_saves_snapshots_although_uvicorn_ends_it_with_its_signal(tmp_path):
    """2026-10-18: the snapshots were written from atexit -- and none was ever written by the app, because uvicorn
    re-raises the SIGTERM it caught once shutdown completes, and the default handler ends the process without atexit."""
    import subprocess
    import sys as _sys
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    r = subprocess.run([_sys.executable, str(script), str(tmp_path)], cwd=os.getcwd(), capture_output=True, text=True,
                       timeout=180)
    assert r.returncode == -15, (r.returncode, r.stderr[-800:])
    assert os.path.exists(tmp_path / "snaps" / "snapexit.snap"), "the app stopped without saving its snapshot"
