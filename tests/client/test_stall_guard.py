"""A chunked transfer that stops moving must end the sweep's wait, not the sweep.

Reported from a tablet: "stuck on 14/2291, never progressing".

`putBlob`/`getBlob` have been bounded for a while. The CHUNKED pair was deliberately left unbounded,
with the reasoning that a file of any size cannot be given a total-time ceiling — which is true — and
that "a stall shows up as no progress, which the resume path already handles", which is not: the
resume path only helps if the sweep ENDS. A socket that dies without an RST leaves an await that
never settles, so the sweep stops on that file for ever, `running` never clears, and Pause cannot
rescue it because `stopping()` is checked between FILES and the sweep is stuck inside one.

The bound is therefore on SILENCE, not on duration: every chunk read and every progress report bumps
it. A transfer that takes an hour is fine. One that has not moved in three minutes is not coming
back, and the sweep is better off recording that file as failed and going on to the next one.

This runs the shipped `_stallGuard` by slicing it out of sync.js — the whole file needs a DOM and a
relay, and the point of this helper is that it needs neither.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNC = os.path.join(ROOT, "static", "js", "client", "sync.js")


def _slice(name):
    """The named function, from `function <name>` to its matching closing brace."""
    src = open(SYNC, encoding="utf-8").read()
    at = src.index("function %s(" % name)
    open_at = src.index("{", at)
    depth, i = 0, open_at
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return src[at:i + 1]


def _const(name):
    src = open(SYNC, encoding="utf-8").read()
    m = re.search(r"const %s = ([^;]+);" % name, src)
    assert m, "%s is gone" % name
    return m.group(1)


def _node(body, timeout=60):
    if shutil.which("node") is None:
        pytest.skip("no node")
    prog = "%s\nconst _STALL_MS = %s;\n%s" % (_slice("_stallGuard"), _const("_STALL_MS"), body)
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_silence_trips_it():
    """The reported failure: nothing moves, and before this the wait was unbounded."""
    got = _node("""
      (async () => {
        const w = _stallGuard('upload', 120);
        const t0 = Date.now();
        let why = '';
        try { await w.tripped; } catch(e){ why = e.message; }
        console.log(JSON.stringify({ ms: Date.now() - t0, why }));
      })();
    """)
    assert got["ms"] >= 100, "it gave up instantly — a slow radio is not a dead one"
    assert "stopped moving" in got["why"]
    assert "try again" in got["why"], "the message has to say it is not final"


def test_progress_keeps_it_alive_however_long_the_file_takes():
    """A big file on a slow link must not be killed by a ceiling. Bumped every 60ms for ~600ms with a
    120ms budget: five times over the limit in total, never once in silence."""
    got = _node("""
      (async () => {
        const w = _stallGuard('upload', 120);
        let tripped = false;
        w.tripped.catch(() => { tripped = true; });
        for (let i = 0; i < 10; i++) {
          await new Promise(r => setTimeout(r, 60));
          w.bump();
        }
        await new Promise(r => setTimeout(r, 20));
        w.stop();
        console.log(JSON.stringify({ tripped }));
      })();
    """)
    assert got["tripped"] is False, "a transfer that was making progress was killed anyway"


def test_stop_disarms_it_for_good():
    """A finished transfer must not trip the guard afterwards — that would report a failure for a
    file that landed, and mark it unsynced on every device."""
    got = _node("""
      (async () => {
        const w = _stallGuard('download', 60);
        let tripped = false;
        w.tripped.catch(() => { tripped = true; });
        w.stop();
        await new Promise(r => setTimeout(r, 200));
        console.log(JSON.stringify({ tripped }));
      })();
    """)
    assert got["tripped"] is False


def test_the_shipped_budget_is_generous_enough_for_one_chunk_on_a_bad_radio():
    """A phone in a basement can take minutes over a 4 MB chunk, and every bump comes at a chunk
    boundary — so a budget in seconds would fail the transfers it exists to protect."""
    ms = eval(_const("_STALL_MS").replace("*", "*"))
    assert ms >= 60_000, "too tight: an ordinary slow chunk would be called dead"
    assert ms <= 10 * 60_000, "too loose: a dead socket should be admitted while somebody is watching"


def test_the_chunked_transfers_actually_use_it():
    """Wiring — the helper is worthless if the pair that hangs does not go through it."""
    src = open(SYNC, encoding="utf-8").read()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    for name in ("putParts", "getParts"):
        m = re.search(r"%s: PC\.syncBlobs[\s\S]{0,700}?\n    (?=\w|\})" % name, src)
        assert m, "%s is no longer wired the way this test reads it" % name
        assert "_stallGuard" in m.group(0), "%s can still hang for ever" % name
