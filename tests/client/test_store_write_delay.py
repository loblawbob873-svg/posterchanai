"""The event cache is written once a minute, not every 0.7 s -- and nothing is lost for it (store_write_delay_runtime.cjs).

Asked 2026-10-28 ("for windows app, android, web browser, any way to delay writes to preserve the disk ?" ->
"why not just write the event cache every min instead of every 0.7s ?"). Measured first on Chrome 155: a commit is
not an fsync (relaxed durability), so the cost is how OFTEN the cache is rewritten, not how durable each write is.
"""
import json
import subprocess
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent / "store_write_delay_runtime.cjs"


def _run():
    r = subprocess.run(["node", str(RUNTIME)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_cache_waits_but_the_accounts_own_documents_do_not_and_hiding_writes_everything():
    got = _run()
    for how in ("visibilitychange", "pagehide"):
        g = got[how]
        assert g["postsAfter1500ms"] == 0, ("somebody else's posts were written within seconds", how, g)
        assert g["noteAfter1500ms"], ("the account's own note waited a minute to be saved", how, g)
        assert g["postsAfterHide"] == 50, ("hiding/closing the page lost the buffered cache", how, g)
    assert got["full"]["written"] == 2000, ("a full buffer was not written early", got["full"])


def test_an_unload_that_loses_the_indexeddb_write_loses_nothing():
    """A write started as the page unloads does not finish (a reload 2 s after viewing a bookmark lost the post).
    pagehide also puts the buffer in localStorage, and the next start restores it."""
    g = _run()["spill"]
    assert g["spilled"] == 30 and g["restored"] == 30 and g["cleared"], g
