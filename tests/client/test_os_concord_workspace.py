"""Concord always receives the full managed desktop workspace, including tab switches."""

from pathlib import Path


OS = (Path(__file__).parents[2] / "static/js/client/os.js").read_text()


def test_switching_an_existing_messages_window_to_concord_maximises_it():
    start = OS.index("function routeView(view, focusOnly)")
    route = OS[start:OS.index("function closeDoc", start)]
    assert "w.appView=view; w.appPath=''; focusWin(w, false);" in route
    assert "if(view==='concord') snapTo(w,'max')" in route
    # ON ENTERING THE TAB, AND NEVER AGAIN. `noteView` is a RENDER notification and Concord
    # re-renders on a four-second tick, so the old unconditional form re-applied `max` every four
    # seconds for as long as the tab was open: un-maximise the Messages window and it snapped back,
    # drag it and it fought you, drag it toward another output and the handoff moved a window whose
    # geometry was being rewritten underneath it. Reported as "can't be moved to another monitor,
    # glitches and resists movement on the monitor it was on".
    #
    # The workspace policy is about how a window OPENS; once it is on screen the geometry belongs to
    # whoever is dragging it. `wasTab` is read BEFORE messagesTab is assigned, which is what makes
    # this a transition rather than a state.
    note = OS[OS.index("function noteView(v)"):OS.index("function ownsFeedView")]
    assert "const wasTab = w.messagesTab;" in note, note
    assert "if(v==='concord' && wasTab!=='concord' && !w.max) snapTo(w,'max')" in note, note


def test_direct_concord_launch_maximises_both_new_and_existing_frames():
    start = OS.index("function openApp(view")
    opened = OS[start:OS.index("function closeWin", start)]
    assert "if(view==='concord') snapTo(existing,'max')" in opened
    assert "if(view==='concord') snapTo(w,'max')" in opened
