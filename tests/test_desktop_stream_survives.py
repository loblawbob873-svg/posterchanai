"""A stream playing in one desktop-mode window must survive activity in another.

Reported, and confirmed by a second user: stream open in one window, Social in another, write a post
and send it — the stream stops. Posting repaints the focused window, `renderView()` runs, and it
called `cleanupInlineStream()` unconditionally, destroying the hls that belongs to a window nobody
touched.

THIS IS THE THIRD BUG OF THE SAME SHAPE, which is why it gets its own file rather than a line in an
existing one. Desktop mode parks an unfocused window by MOVING its nodes into that window's own slot,
where they stay live and on screen — so any module-level singleton reached through `$('#thing')` or a
bare global is now reachable by a window that does not own it:

  * the timeline prepended live posts into whichever window held `#feed` (check_os_desktop's
    stale-view assertion);
  * `flushLive` destroyed a parked window's buffered posts, then routed them to a pill that is
    force-hidden while parked (tests/test_desktop_parked_feed.py);
  * and this one, which stops somebody's video.

The shared question each time is "does this still belong to me?", and the answer is always in the
DOM: what is in the live `#feed` is this window's, what is in an `.osw-slot` is another's.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()


def _fn(name):
    i = APP.index("  function %s(" % name)
    return APP[i:APP.index("\n  }", i) + 4]


def test_leaving_a_view_does_not_stop_another_windows_stream():
    body = _fn("cleanupInlineStream")
    assert "_streamParked()" in body, \
        "renderView tears this down on every view change; parked, that is another window's player"
    assert body.index("_streamParked()") < body.index("_disposeInlineHls"), \
        "the guard must come before the teardown, not after it"


def test_the_guard_asks_which_window_owns_the_player():
    """`getElementById` answers with whichever element it finds first and cannot tell two open stream
    windows apart. The live #feed is the one this window owns."""
    body = _fn("_streamParked")
    assert "'#feed #st-video'" in body, "ownership is 'is there a player in the LIVE feed'"
    assert "'.osw-slot #st-video'" in body, "…and parked is 'there is one in somebody's slot'"
    assert "getElementById('st-video')" not in body, \
        "a first-match lookup cannot distinguish two stream windows"


def test_it_is_inert_outside_desktop_mode():
    """Classic mode has one view at a time, and the old unconditional teardown was right there. The
    guard must not change it."""
    body = _fn("_streamParked")
    assert "if(!window.PCOS) return false;" in body


def test_attaching_a_new_player_still_disposes_the_old_one():
    """The one caller that is REPLACING the player it owns must not be blocked by the guard, or every
    stream opened in a window leaks the previous hls."""
    assert "function _disposeInlineHls(" in APP
    i = APP.index("_disposeInlineHls();   // drop any previous inline hls")
    # …and it is the raw one, not the guarded wrapper.
    assert "cleanupInlineStream();   // drop any previous" not in APP
    assert i > 0


def test_the_teardown_still_happens_on_an_ordinary_view_change():
    """The guard must not turn this into a no-op — a stream left playing after you navigate away in
    classic mode is a socket and a decoder nobody is watching."""
    assert re.search(r"cleanupInlineStream\(\);\s*//\s*leaving a view tears down", APP), \
        "renderView no longer tears the inline stream down at all"
