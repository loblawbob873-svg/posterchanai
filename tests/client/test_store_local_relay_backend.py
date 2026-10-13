"""The client's Store uses PosterChanOS's local relay as its disk, and IndexedDB whenever it cannot.

Runs the SHIPPED store.js (store_local_relay_runtime.cjs) against a stub local relay and a stub IndexedDB:
  up            -> events go to the relay as EVENT frames; only the pinned note is ALSO written to IndexedDB;
                   the boot hydrate brings what the relay holds into memory;
  down          -> (connection refused at boot) every event lands in IndexedDB, nothing is lost;
  no-relay / not-loopback -> no URL, or a URL that is not ws:// on loopback, is ignored: IndexedDB;
  refused       -> what the relay refuses is written to IndexedDB (and is not re-offered in a loop);
  dies-holding  -> a relay that dies holding unacknowledged events gives every one of them to IndexedDB.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run():
    out = subprocess.run(["node", str(HERE / "store_local_relay_runtime.cjs")], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-3000:]
    return {r["name"]: r for r in json.loads(out.stdout)}


def test_the_store_writes_through_the_local_relay_and_falls_back_to_indexeddb():
    r = _run()
    up = r["up"]
    assert up["backend"] == "relay" and up["frames"] == 51, up
    assert up["idbEvents"] == 1 and up["idbHasNote"], ("with the relay up only the pinned note may go to IndexedDB", up)
    assert up["hydrated"] == 2, ("the boot hydrate did not read the relay's events into memory", up)
    for name in ("down", "no-relay", "not-loopback"):
        x = r[name]
        assert x["backend"] == "idb" and x["frames"] == 0 and x["idbEvents"] == 51 and x["idbHasNote"], (name, x)
    ref = r["refused"]
    assert ref["frames"] == 51 and ref["idbEvents"] == 51 and ref["stats"]["refused"] == 51, ref
    dead = r["dies-holding"]
    assert dead["idbEvents"] == 51 and dead["stats"]["fellBack"] == 51 and dead["frames"] == 51, dead
