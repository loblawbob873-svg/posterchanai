"""A timeline nobody is looking at must not be subscribed — and NOTHING ELSE may be closed with it.

Run: venv-unified/bin/python -m pytest tests/test_timeline_view_park.py

tests/test_timeline_background_pause.py covers the other half of the same question: "is this APP on
screen?". This one is "is this TIMELINE on screen?", and for the life of the client nothing asked it.
renderTimeline closes and re-opens the subscription for the view being ENTERED; nothing ever closed
the one being LEFT. So one visit to the timeline left the firehose REQ open for the rest of the
session — every kind-1/6/1068/30023/34550/40 on the relay streaming into a Store nothing was
painting, while the user sat in Notes, the Vault, Files or Calendar — and in classic mode Home →
Nostrverse never closed Home, so the app ran TWO firehoses to paint neither.

None of that raises anything, which is why it needs a test rather than a look: the screen you ARE on
is correct the whole time, and the cost is radio, battery and CPU on a phone in a pocket.

The two failure directions, both asserted below:

  * TOO LITTLE — the sub survives the navigation (the bug), or is dropped only from renderView, which
    renderThread and renderProfile do not go through.
  * TOO MUCH  — a desktop window that is PARKED is still on screen, so tearing its timeline down
    reads as "the Social window stopped updating whenever I click another window"; and closing
    anything beyond home/global would take notifications, DMs, calls or the signer with it, which is
    the one thing the user asked not to break.
"""
import json
import re
import shutil
import subprocess

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")

def _fn_source():
    """The shipped functions plus the one constant they read, sliced out for the node runs below."""
    views = re.search(r"const _TL_SUB_VIEWS = \[[^\]]*\];", APP)
    assert views, "_TL_SUB_VIEWS is gone — re-point this test"
    park = re.search(r"function _parkOffscreenTimelines\(\)\{.*?\n  \}", APP, re.S)
    assert park, "_parkOffscreenTimelines moved — re-point this test"
    vis = re.search(r"function _visibleSlot\(view\)\{.*?\n  \}", APP, re.S)
    assert vis, "_visibleSlot moved — re-point this test"
    return views.group(0) + "\n" + park.group(0) + "\n" + vis.group(0)


def test_it_only_ever_touches_the_two_timeline_subs():
    """The safety argument for the whole change. Notifications, DMs, calls, the signer, Notes and the
    vault are their own subscriptions; if this ever learns another key, a like stops arriving while
    you are in Notes and nothing says so."""
    views = re.search(r"const _TL_SUB_VIEWS = (\[[^\]]*\]);", APP)
    assert views, "_TL_SUB_VIEWS is gone — re-point this test"
    assert json.loads(views.group(1).replace("'", '"')) == ["home", "global"]
    body = re.search(r"function _parkOffscreenTimelines\(\)\{(.*?)\n  \}", APP, re.S).group(1)
    assert "_TL_SUB_VIEWS" in body, "the set of closable subs is a literal again"
    # It closes by id out of `subs`, so it cannot reach a sub that isn't in there at all.
    assert "subs[v]" in body and "Relay.close(id)" in body


def test_a_parked_desktop_window_keeps_its_timeline():
    """Parked is not hidden: os.js moves an unfocused window's DOM into its own slot, where it is
    still on screen and still being prepended into. This is the assertion that keeps "battery on a
    phone" from becoming "the Social window freezes when I click another window"."""
    body = re.search(r"function _parkOffscreenTimelines\(\)\{(.*?)\n  \}", APP, re.S).group(1)
    assert "_visibleSlot(v)" in body, "a parked desktop timeline is torn down"
    # …and _visibleSlot is _parkedSlot plus the minimised question, so the two modules still agree
    # about WHERE a view lives; only the "can anyone see it" half is asked here.
    vis = re.search(r"function _visibleSlot\(view\)\{(.*?)\n  \}", APP, re.S).group(1)
    assert "_parkedSlot(view)" in vis and "osw.minimised" in vis
    # Same question, same helper as _flushLiveFor asks — so the two cannot disagree about what
    # "on screen" means and start closing a sub whose posts are still being drawn.
    assert "_tlParked()" not in body, (
        "_tlParked() is 'VIEW is not a timeline', which is false whenever the OTHER timeline is in "
        "front — it cannot answer this question for a specific view")


def test_a_minimised_window_is_put_away_not_parked():
    """os.js parks an unfocused window AND a minimised one the same way, so `_parkedSlot` answers
    'yes, there is a slot' for both. Only one of them is on screen. Held open, a minimised Social
    window kept the whole Nostrverse streaming behind a taskbar button."""
    vis = re.search(r"function _visibleSlot\(view\)\{(.*?)\n  \}", APP, re.S).group(1)
    assert "closest('.osw.minimised')" in vis
    # The class the check reads has to be the one os.js actually writes.
    osjs = (ROOT / "static" / "js" / "client" / "os.js").read_text(encoding="utf-8")
    assert "classList.add('minimised')" in osjs, "os.js no longer marks minimised windows that way"
    assert "$('.osw-slot', el)" in osjs, "the slot is no longer inside the window element"


def test_restoring_a_minimised_timeline_re_arms_it():
    """The quiet path is the ONE path that never reaches renderTimeline — os.js restores a parked
    window's real DOM and skips the repaint. Right for every window except a timeline whose sub we
    dropped: that one comes back correct and frozen, with nothing left to re-subscribe it."""
    sv = APP[APP.index("function switchView(v, quiet){"):]
    sv = sv[: sv.index("\n  function renderView(reset){")]
    assert "else if(_TL_SUB_VIEWS.includes(v) && !subs[v]) renderView(false);" in sv, (
        "a minimised timeline window comes back frozen")
    # reset=true would replace the restored cards with a spinner and throw away the scroll os.js is
    # about to restore.
    assert "renderView(true)" in sv and "renderView(false)" in sv


def test_both_legs_are_wired():
    """renderView covers the sidebar. The onEvent leg is the one that cannot be forgotten: renderThread
    and renderProfile set VIEW themselves and never go through renderView, and os.js can close a
    window without one either."""
    assert APP.count("_parkOffscreenTimelines()") >= 3, (
        "expected the definition plus both call sites (renderView and the timeline's onEvent)")
    render_view = APP[APP.index("function renderView(reset){"):]
    render_view = render_view[: render_view.index("if (VIEW==='home' || VIEW==='global')")]
    assert "_parkOffscreenTimelines();" in render_view, "navigating away no longer drops the firehose"
    on_event = APP[APP.index("const onEvent = ev => {"):]
    on_event = on_event[: on_event.index("const markEosed")]
    assert "_parkOffscreenTimelines()" in on_event, (
        "a view that sets VIEW without renderView leaves the firehose open for the session")
    assert "VIEW!==view && !_parkedSlot(view)" in on_event


def test_renderview_does_not_hide_it_behind_the_desktop_guard():
    """renderView skips the Notes/vault/terminal teardowns when PosterChan OS is on, because there a
    view lives in its own window. This one must NOT be skipped — it has its own, finer answer
    (`_parkedSlot`), and a desktop window CLOSED rather than parked would otherwise leak its
    subscription for the rest of the session."""
    render_view = APP[APP.index("function renderView(reset){"):]
    render_view = render_view[: render_view.index("if (VIEW==='home' || VIEW==='global')")]
    call = render_view.index("_parkOffscreenTimelines();")
    guard = render_view.index("if(!(window.PCOS && PCOS.isOn())){")
    assert call < guard, "the park call moved inside the classic-mode-only block"


def test_the_desktop_does_not_open_social_on_its_own():
    """A remembered desktop used to come up with a Social window in front of its own icons and the
    Nostrverse firehose already streaming, before a single click — because the boot landing goes
    through PCOS.routeView, which CONJURES a window for the view it is handed. The desktop's home
    screen is the icon grid; "nothing is open" is a state it has and classic mode does not."""
    boot = APP[APP.index("if(_deepLink){ VIEW='thread';"):]
    boot = boot[: boot.index("_consumeSharedFiles()")]
    assert "PCOS.isOn()" in boot, "the landing still materialises a window on the desktop"
    # THE SHAPE, NOT THE FUNCTION'S NAME. This asserted the literal `_startTimeline()`, which is
    # what the landing called until the "screen the app opens on" preference arrived and it became
    # `_startView()`. The rule never changed — off the desktop the landing switches to the chosen
    # screen and marks itself — but the test read as a regression in the landing for weeks.
    assert re.search(r"switchView\(_start\w*\(\)\);\s*_onLandingView\s*=\s*true;", boot), (
        "off the desktop the landing must still switch to the start screen and mark itself")


def test_the_landing_guard_is_a_question_and_never_a_latch():
    """The previous boot-landing guard (`_viewChosen`) was a latch, and `applyInstanceGating` can
    switchView during boot — which made the landing skip ITSELF and shipped a broken APK. This one
    reads the screen at the moment of landing, so nothing else running during boot can set it."""
    boot = APP[APP.index("if(_deepLink){ VIEW='thread';"):]
    boot = boot[: boot.index("_consumeSharedFiles()")]
    # Comments stripped first: the reason this guard is shaped the way it is names the old latch.
    code = re.sub(r"/\*.*?\*/", "", APP, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    assert "_viewChosen" not in code, "the reverted latch is back"
    # The flag is computed inside the landing branch and read once — never assigned anywhere else.
    assert len(re.findall(r"_osHome", APP)) == 3, (
        "_osHome is written somewhere other than the landing that computes it")
    # And `_onLandingView` must stay unset when there is no landing view, or a late synced pref
    # would open a window of its own through restoreClientPrefsNostr.
    assert re.search(r"if\(!_osHome\)\{\s*switchView\(_start\w*\(\)\);\s*_onLandingView\s*=\s*true;\s*\}",
                     boot), ("the landing must stay guarded by _osHome and set the flag only there")


@pytest.mark.skipif(not shutil.which("node"), reason="node is what runs the shipped function")
def test_the_shipped_function_against_every_arrangement():
    """Not a grep: the real function, driven through the four states it has to tell apart.

    A stand-in `subs` carrying both timelines AND a notifications sub, so "closes too much" fails
    here as a wrong answer rather than as a crash."""
    script = """
      let VIEW = null, parked = null, minimised = false;
      const closed = [];
      const Relay = { close: (id) => closed.push(id) };
      // A stand-in slot that answers `closest` the way the real one does: it is a descendant of the
      // window element, so it finds `.osw.minimised` exactly when that window is minimised.
      const _parkedSlot = (v) => (parked === v
        ? { closest: (sel) => (sel === '.osw.minimised' && minimised ? { el:true } : null) }
        : null);
      let subs = {};
      %s
      const run = (view, park, min) => {
        VIEW = view; parked = park; minimised = !!min; closed.length = 0;
        subs = { home:'sub-home', global:'sub-global', notifications:'sub-notif', dms:'sub-dm' };
        _parkOffscreenTimelines();
        return { closed: closed.slice().sort(),
                 left: Object.keys(subs).filter(k => subs[k]).sort() };
      };
      console.log(JSON.stringify({
        onHome:        run('home', null),
        onNotes:       run('notes', null),
        onGlobal:      run('global', null),
        notesButHomeParked:    run('notes', 'home', false),
        notesButHomeMinimised: run('notes', 'home', true),
      }));
    """ % _fn_source()
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)

    # Sitting on Home: the OTHER timeline is still open from an earlier visit and is pure waste.
    assert got["onHome"]["closed"] == ["sub-global"]
    assert got["onHome"]["left"] == ["dms", "home", "notifications"]

    # In Notes: BOTH firehoses go, and nothing else does. This is the reported case.
    assert got["onNotes"]["closed"] == ["sub-global", "sub-home"]
    assert got["onNotes"]["left"] == ["dms", "notifications"], (
        "notifications or DMs were closed with the timeline — likes and replies would stop arriving")

    assert got["onGlobal"]["closed"] == ["sub-home"]

    # Desktop: in Notes, with the Social window parked beside it and still on screen.
    assert got["notesButHomeParked"]["closed"] == ["sub-global"]
    assert "home" in got["notesButHomeParked"]["left"], "a visible parked timeline lost its posts"

    # …and the same window MINIMISED, which os.js parks identically and nobody can see.
    assert got["notesButHomeMinimised"]["closed"] == ["sub-global", "sub-home"], (
        "a minimised Social window keeps the Nostrverse streaming behind a taskbar button")


@pytest.mark.skipif(not shutil.which("node"), reason="node is what runs the shipped function")
def test_the_check_can_fail():
    """The pre-fix rule, run through the same harness: keying on VIEW alone (no _parkedSlot) tears
    down a desktop window that is on screen. If this ever passes, the assertion above is not
    measuring what it claims to."""
    broken = _fn_source().replace("if(_visibleSlot(v)) continue;", "")
    assert "if(_visibleSlot(v)) continue;" in _fn_source(), "the guard moved — this proves nothing"
    script = """
      let VIEW = 'notes';
      const closed = [];
      const Relay = { close: (id) => closed.push(id) };
      const _parkedSlot = (v) => (v === 'home' ? { closest: () => null } : null);
      let subs = { home:'sub-home', global:'sub-global' };
      %s
      _parkOffscreenTimelines();
      console.log(JSON.stringify(closed.sort()));
    """ % broken
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == ["sub-global", "sub-home"], (
        "dropping the parked check no longer changes the answer — the guard above proves nothing")
