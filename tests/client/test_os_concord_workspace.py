"""Concord always receives the full managed desktop workspace, including tab switches."""

from pathlib import Path


OS = (Path(__file__).parents[2] / "static/js/client/os.js").read_text()


def test_switching_an_existing_messages_window_to_concord_maximises_it():
    start = OS.index("function routeView(view, focusOnly)")
    route = OS[start:OS.index("function closeDoc", start)]
    assert "w.appView=view; w.appPath=''; focusWin(w, false);" in route
    assert "if(view==='concord') snapTo(w,'max')" in route
    note = OS[OS.index("function noteView(v)"):OS.index("function ownsFeedView")]
    assert "if(v==='concord' && !w.max) snapTo(w,'max')" in note


def test_direct_concord_launch_maximises_both_new_and_existing_frames():
    start = OS.index("function openApp(view")
    opened = OS[start:OS.index("function closeWin", start)]
    assert "if(view==='concord') snapTo(existing,'max')" in opened
    assert "if(view==='concord') snapTo(w,'max')" in opened
