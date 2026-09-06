"""`String(aPromise)` IS "[object Promise]", AND copyValue STRINGIFIES WHATEVER IT IS HANDED.

A bug report arrived reading, in full:

    Fedi Bridge Bug: fix this in new thread:[object Promise]

-- the person had copied something in the app and pasted those sixteen characters. One caller that
forgets an `await` does not throw and does not log; it silently replaces the clipboard, and the
person finds out when they paste into somebody else's window, by which point what they had is gone
too.

`copyValue` is the single choke point every copy in the app goes through (the APK's WebView and the
desktop's app:// origin both refuse `navigator.clipboard`, which is why it exists), so the guard
belongs here and nowhere else. A thenable is AWAITED -- that is the caller's intent, merely late --
and any other object is refused out loud rather than pasted as "[object Object]".

The shipped function is RUN here, against a stub of each clipboard route, because a source-text
assertion would pass against a guard wired to the wrong branch.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run():
    out = subprocess.run(["node", str(ROOT / "tests/client/copy_value_sim.mjs")],
                         cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_promise_is_awaited_not_stringified():
    r = _run()
    assert "[object Promise]" not in r["written"], (
        f"the clipboard was given {r['written']!r}")
    assert r["resolved"] == "note1realvalue", r


def test_a_plain_object_is_refused_out_loud():
    r = _run()
    assert r["objectWritten"] is None, "an object was pasted into the clipboard"
    assert r["objectToast"], "refusing to copy said nothing to the person"
    assert "[object Object]" not in (r["objectWritten"] or "")


def test_ordinary_values_are_untouched():
    r = _run()
    assert r["plain"] == "note1plain"
    assert r["number"] == "42"
    assert r["empty"] == "", "an empty string must still clear/write, not be refused as an object"


def test_a_rejected_promise_does_not_write_anything():
    r = _run()
    assert r["rejectedWritten"] is None
    assert r["rejectedToast"]
