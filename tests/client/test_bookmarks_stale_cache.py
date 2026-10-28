"""A stale cached bookmark list is not one a relay confirmed (bookmarks_stale_cache_runtime.mjs).

The gate's real-node test failed "both bookmarks listed after a reload" for a dozen deploys, always with the OLDER
list in the Store: the cached apply marked the list as read, so a missed relay read was never asked again.
"""
import json
import subprocess
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent / "bookmarks_stale_cache_runtime.mjs"


def test_a_missed_read_after_a_reload_is_asked_again_until_the_newer_list_arrives():
    out = subprocess.run(["node", str(RUNTIME)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["bookmarks"] == ["a1", "a2"], ("the newer list on the relay was never read", got)
    assert got["shown"] == 2, ("one of two bookmarks listed after a reload", got)
