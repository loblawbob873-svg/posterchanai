"""No IndexedDB request callback may be `async`, because the transaction dies underneath it.

Run: venv-unified/bin/python -m pytest tests/client/test_idb_cursor_never_awaits.py

An IndexedDB transaction stays alive only while it has a pending request. The moment an `async`
`onsuccess` yields — on a `crypto.subtle` call, a fetch, anything — there is none, so the transaction
AUTO-COMMITS. When the await resolves, the cursor's transaction has finished and `c.continue()`
throws `Failed to execute 'continue' on 'IDBCursor'`.

WHAT MADE IT WORTH A TEST IS HOW QUIET IT IS. The throw is inside an async handler nobody awaits, so
it is not an error any `try` catches — it is an unhandled rejection, which this app turns into a
toast reading **"action failed"**, naming no action. `pushShared` runs on startup, so a user with
anything in the DM cache got that toast on every launch of the Windows app. And the walk stops at the
first record, so the shared DM cache was never pushed at all: a silent feature outage wearing a
frightening and uninformative toast.

It is also invisible to every other kind of test here. The behaviour depends on the real IndexedDB
transaction lifetime, which no stub reproduces — a fake store with a synchronous cursor iterates
perfectly. The one thing that reliably distinguishes it is the SHAPE: `async` on a callback that IDB
itself invokes. So that is what is checked.

The rule is narrow on purpose: collect synchronously inside the walk, do the async work after the
transaction has closed. That is the standard shape and it costs nothing here.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "static" / "js" / "client"
FILES = sorted(CLIENT.glob("*.js"))

# The callbacks IndexedDB invokes itself, i.e. the ones running inside a live transaction.
IDB_CALLBACKS = ("onsuccess", "onupgradeneeded", "oncomplete", "oncursor")


def _strip(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def test_no_idb_callback_is_async():
    bad = []
    for path in FILES:
        src = _strip(path.read_text(encoding="utf-8"))
        for cb in IDB_CALLBACKS:
            for m in re.finditer(r"\.%s\s*=\s*async\b" % cb, src):
                bad.append("%s:%d %s" % (path.name, src.count("\n", 0, m.start()) + 1, cb))
    assert not bad, (
        "an IndexedDB callback is async — the transaction auto-commits at the first await and the "
        "next cursor.continue() throws 'Failed to execute continue on IDBCursor', as an UNHANDLED "
        "rejection (a bare 'action failed' toast). Collect synchronously, then await after the walk: "
        + repr(bad))


def test_the_cursor_walk_that_broke_collects_before_it_decrypts():
    """The specific one, pinned by shape rather than by line: the walk pushes raw records and the
    decrypt happens after the promise resolves."""
    src = _strip((CLIENT / "app.js").read_text(encoding="utf-8"))
    i = src.index("async pushShared(){")
    body = src[i: i + 3000]
    walk = body[body.index("openCursor()"): body.index("q.onerror")]
    assert "await" not in walk, "pushShared awaits inside the cursor walk again: " + walk[:300]
    assert "rawRecs.push" in walk, "the walk no longer collects — re-point this test"
    after = body[body.index("q.onerror"):]
    assert "crypto.subtle.decrypt" in after, "the decrypt moved back inside the transaction"


def test_the_check_can_fail():
    """The pre-fix line, through the same rule."""
    bad = re.findall(r"\.onsuccess\s*=\s*async\b", _strip("          q.onsuccess = async () => {"))
    assert bad, "the pattern no longer matches the code that shipped — the guard above is inert"
