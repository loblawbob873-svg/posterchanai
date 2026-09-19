"""DOUBLE-HOME MUST NOT "SHAKE" THE FEED (backlog §2).

Android restores a WebView's saved scroll offset as the Activity becomes visible, AFTER onNewIntent.
If `timelineTop()` runs from the intent (while the page is still hidden) the feed reloads and the
platform then writes the old offset back over it — the visible shake. `settleFeedTop` in
phoneshell.js defers: while `document.visibilityState === 'hidden'` it does nothing, and only once
the page is visible does it schedule a single short (180 ms) foreground turn that runs `timelineTop`
exactly once.

This RUNS the shipped `settleFeedTop` under node with a controllable timer and a spy `timelineTop`,
so a mutation to the visibility guard fails here.
"""
import json
import shutil
import subprocess
from pathlib import Path

PHONE = (Path(__file__).parents[2] / "static/js/client/phoneshell.js").read_text(encoding="utf-8")
NODE = shutil.which("node") or shutil.which("nodejs")


def _function(name):
    start = PHONE.index(f"function {name}(")
    brace = PHONE.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for pos in range(brace, len(PHONE)):
        char = PHONE[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return PHONE[start:pos + 1]
    raise AssertionError(f"unterminated {name}")


def _run(body):
    script = (
        "let _feedTopWaiting=true,_feedTopTimer=0,calls=0;\n"
        "const PC={ timelineTop:()=>{ calls++; } };\n"
        "const document={ visibilityState:'hidden' };\n"
        "let timers=[];\n"
        "const setTimeout=(fn,ms)=>{ const id=timers.length+1; timers.push({id,fn,ms}); return id; };\n"
        "const clearTimeout=(id)=>{ timers=timers.filter(t=>t.id!==id); };\n"
        "const fireTimers=()=>{ const t=timers.slice(); timers=[]; t.forEach(x=>x.fn()); };\n"
        + _function("settleFeedTop") + "\n"
        "const out={};\n" + body + "\n"
        "process.stdout.write(JSON.stringify(out));\n")
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-600:]
    return json.loads(r.stdout)


def test_a_hidden_page_never_refreshes_the_feed():
    assert NODE, "no node"
    out = _run(
        "settleFeedTop();\n"
        "out.scheduledWhileHidden = timers.length;\n"      # hidden → nothing scheduled at all
        "fireTimers();\n"
        "out.callsWhileHidden = calls;\n")                 # …and nothing ran
    assert out["scheduledWhileHidden"] == 0, "a hidden page scheduled a feed refresh — the shake"
    assert out["callsWhileHidden"] == 0, "timelineTop ran while the page was hidden"


def test_a_visible_page_refreshes_exactly_once():
    assert NODE, "no node"
    out = _run(
        "document.visibilityState='visible';\n"
        "settleFeedTop();\n"
        "out.scheduled = timers.length;\n"                 # visible → one short foreground turn
        "fireTimers();\n"
        "out.calls = calls;\n"
        "out.waiting = _feedTopWaiting;\n")
    assert out["scheduled"] == 1, "a visible page did not schedule the deferred refresh"
    assert out["calls"] == 1, "the feed was not refreshed exactly once when visible"
    assert out["waiting"] is False, "the waiting latch was not cleared after firing"


def test_going_hidden_before_the_turn_elapses_cancels_the_refresh():
    """The 180ms turn re-checks visibility: if the app went back to the background before it fired,
    the refresh must not run (a resume that was immediately re-backgrounded must not shake later)."""
    assert NODE, "no node"
    out = _run(
        "document.visibilityState='visible';\n"
        "settleFeedTop();\n"                               # schedules the inner turn
        "document.visibilityState='hidden';\n"            # backgrounded again before it fires
        "fireTimers();\n"
        "out.calls = calls;\n")
    assert out["calls"] == 0, "the feed refreshed after the app was backgrounded again"
