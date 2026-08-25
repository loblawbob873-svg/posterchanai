"""A PARKED timeline window is on screen, and must keep receiving posts.

Desktop mode hands `id="feed"` to whichever window has focus and PARKS the rest — os.js MOVES their
nodes into the window's own `.osw-slot` rather than copying them, so a parked window is still the
real, live DOM, still visible, and still scrollable. It is parked because something else has focus,
not because it is hidden.

Two bugs in a row came out of that. The first: `flushLive` returned early whenever VIEW was not a
timeline view, and the buffer had already been drained by the splice above it — so the posts were
DESTROYED, and since markEosed only draws on the FIRST EOSE nothing ever backfilled them. The second,
which is what this file is about: routing them to the pending list instead. That looks correct and
is not, because the "↑ N new posts" pill that releases them floats over `.main` — the FOCUSED
window. So a visible feed sat frozen for as long as you were reading something else, with its
release control drawn on top of a different window. Reported as "Desktop Mode Social app not
updating with new posts when not focused".

Asserted here rather than in scripts/check_os_desktop.py because that harness loads os.js against a
stub feature and never evaluates app.js, so flushLive does not exist in it.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
OS_JS = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "css", "client.css"), encoding="utf-8").read()


def _parked_branch(code_only=True):
    """The body of `if(_tlParked()){ … }` inside flushLive.

    Comments stripped by default: this branch DOCUMENTS the two mechanisms it must not use
    (`_livePending`, `$('#feed')`), so a plain substring search over the source finds the very names
    it is warning against and every negative assertion here would pass vacuously — or fail wrongly.
    """
    i = APP_JS.index("function _flushLiveFor(")
    j = APP_JS.index("if(VIEW!==view){", i)
    depth = 0
    for k in range(APP_JS.index("{", j), len(APP_JS)):
        if APP_JS[k] == "{":
            depth += 1
        elif APP_JS[k] == "}":
            depth -= 1
            if depth == 0:
                src = APP_JS[j:k + 1]
                if not code_only:
                    return src
                src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
                return re.sub(r"//[^\n]*", "", src)
    raise AssertionError("could not read the parked branch")


def test_a_parked_timeline_prepends_rather_than_only_stashing():
    b = _parked_branch()
    assert "_prependLive(" in b, \
        "a parked window is visible — its posts must be drawn, not held behind a pill on another window"


def test_it_prepends_into_the_parked_window_and_not_the_focused_feed():
    """`#feed` belongs to whatever has focus. Prepending there would put timeline posts into a
    Profile or Post window — the exact bug check_os_desktop calls stale-view."""
    b = _parked_branch()
    assert "_parkedSlot(view)" in b, \
        "the parked window's own slot is the target; $('#feed') is a different window entirely"
    assert "_prependLive(evs, slot)" in b, "must prepend into the slot, not the focused feed"
    assert "$('#feed')" not in b and 'getElementById("feed")' not in b


def test_the_pending_list_is_never_used_while_parked():
    """THE SECOND ATTEMPT'S BUG, and it left the reported symptom intact.

    `_updateNewPostsPill` counts `(VIEW==='home'||VIEW==='global') ? _livePending.length : 0`, and
    `_tlParked()` is true only when VIEW is NEITHER — so every call from this path resolves to zero
    and HIDES the pill. Posts sent there are invisible with no control to release them, and
    refocusing runs `_resetLive()`, which empties `_livePending` outright: destroyed, not delayed.

    The pill could not serve a parked window in any case. It floats over `.main` — the FOCUSED
    window — and `_flushPending` prepends into `$('#feed')`, so clicking it would pour timeline posts
    into whatever is in front. Mixing the two is worse than either: posts drawn here while older ones
    waited in the list came out of ORDER, because _flushPending inserts its batch at `firstChild`.
    """
    b = _parked_branch()
    assert "_livePending" not in b, \
        "a parked window cannot use the pill — its count is forced to 0 and _resetLive wipes the list"
    assert "_updateNewPostsPill" not in b


def test_the_pill_really_is_blind_while_parked():
    """The premise above, asserted against the real function so it cannot quietly stop being true.

    Asserted as the RULE rather than as one spelling of it. The literal this used to match broke the
    day the pill gained an off switch — a change with nothing whatever to do with parked windows —
    and a test that fails for the wrong reason teaches people to edit the test."""
    fn = APP_JS[APP_JS.index("function _updateNewPostsPill(){"):]
    fn = fn[:fn.index("\n  }")]
    assert "(VIEW==='home'||VIEW==='global')" in fn and "_livePending.length:0" in fn, \
        "the pill's count must still be forced to 0 outside home/global"
    # NOT the bare word "hidden" — the pill shows and hides itself with that CSS class, which is
    # the function doing its job, not the function learning where it is.
    for parked in ("parked", "_tlPark", "document.hidden", "visibilityState"):
        assert parked not in fn, (
            f"the pill now consults {parked!r} — if it can tell it is parked, the parked branch can "
            f"start using it again, and a batch inserted at firstChild comes out in the wrong order")


def test_the_media_grid_is_still_excluded():
    b = _parked_branch()
    assert "_tlMedia" in b, "the media grid does not take live prepends on any path"


def test_posts_are_never_dropped_on_the_parked_path():
    """Reading position is protected by _prependLive itself, which measures scrollHeight before and
    after and corrects scrollTop by the difference — so nothing has to be withheld to keep a parked
    window steady, and withholding is what loses posts."""
    assert "if(!atTop) feed.scrollTop += (feed.scrollHeight - beforeH);" in APP_JS, \
        "_prependLive lost its scroll correction — the parked path depends on it"


# ---- the invariants the fix rests on -----------------------------------------------------------

def test_parking_moves_the_nodes_rather_than_copying_them():
    """If a parked window ever held a serialised COPY again, prepending into it would paint into a
    dead node with no handlers — and the live posts would be unclickable rather than missing."""
    assert "while(realFeed.firstChild) slot.appendChild(realFeed.firstChild);" in OS_JS, \
        "park no longer MOVES the feed's nodes into the slot"
    # Comments stripped first: os.js documents the old serialising line by quoting it, so a plain
    # substring search over the source finds the very thing it is warning against.
    code = re.sub(r"/\*.*?\*/", "", OS_JS, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    assert "slot.innerHTML = realFeed.innerHTML" not in code, \
        "park is serialising the feed again — a parked window would be a dead copy"


def test_every_click_focuses_its_window_before_the_apps_handler_runs():
    """`.click()`, keyboard activation and accessibility tools can fire click without pointerdown.
    Capture is load-bearing: a bubbling app handler may render immediately, and while another
    window owns the shared feed that turns Terminal/Code into Blossom or Concord."""
    needle = "el.addEventListener('click', () => {"
    start = OS_JS.index(needle, OS_JS.index("function openApp("))
    body = OS_JS[start:OS_JS.index("}, true);", start) + len("}, true);")]
    assert "focusWin(w)" in body
    assert body.endswith("}, true);"), "the focus fallback must run in capture before app handlers"


def test_the_slot_is_the_scroller():
    """_prependLive measures scrollTop/scrollHeight on the element it is handed. If .osw-slot stops
    being the overflow box, the scroll-stability correction silently applies to the wrong element."""
    m = re.search(r"\.osw-slot\{([^}]*)\}", CSS)
    assert m, ".osw-slot rule is gone"
    assert "overflow:auto" in m.group(1), \
        ".osw-slot is no longer the scroller — the parked prepend measures the wrong element"


def test_the_right_window_is_targeted_when_two_timelines_are_open():
    """Parking MOVES nodes into the slot, so Home and Nostrverse both open puts TWO elements with
    `id="tl-notes"` in one document — and getElementById answers with whichever opened first. That
    would prepend the firehose into the following-feed while the window it belongs to never updates.
    Only os.js knows which window is which, so it answers, and the view travels with the buffer."""
    assert "function parkedSlot(view)" in OS_JS, "os.js must expose the view→slot lookup"
    assert "parkedSlot," in OS_JS, "…and export it on window.PCOS"
    assert "(x.appView || x.view) === view" in OS_JS, \
        "match on what the window was SHOWING when parked; a window navigated inside itself moved on"
    assert "_bufferLive(ev, fn, view)" in APP_JS, "the view has to travel with the buffered events"
    fb = APP_JS[APP_JS.index("function _parkedSlotDom(){"):]
    fb = fb[:fb.index("\n  }")]
    assert "all.length===1" in fb, \
        "the DOM fallback must refuse to guess when two timelines are open"
    # …and os.js's answer, including its "no", must be final when it can give one.
    main = APP_JS[APP_JS.index("function _parkedSlot(view){"):]
    main = main[:main.index("\n  }")]
    assert "PCOS.parkedSlot ? PCOS.parkedSlot(view) : _parkedSlotDom()" in main, \
        "the DOM fallback must only cover an os.js that predates parkedSlot, never override its refusal"


def test_tl_parked_still_identifies_a_parked_timeline():
    assert "function _tlParked(" in APP_JS
    body = APP_JS[APP_JS.index("function _tlParked("):]
    body = body[:body.index("\n  function ")]
    assert "getElementById('tl-notes')" in body and "window.PCOS" in body, \
        "the parked test must stay 'the timeline DOM exists while VIEW names another window'"


def test_a_flush_needs_a_scroll_a_person_did():
    """Reported on Android: with the "new posts" button turned off, scroll down, lock the screen,
    unlock — and the app is back at the top of the timeline.

    `onFeedScroll` flushes the buffered posts whenever `scrollTop` is near the top, and there is one
    moment when that reading lies: the page coming back from backgrounded. Android restores the
    WebView's scroll offset AFTER layout, so an unlock fires a scroll event while the offset still
    reads 0 — indistinguishable from somebody scrolling up — and the flush inserts the whole buffer
    above where they were reading.

    The buffer being large is what makes it violent, and it is largest with the button turned off:
    with the button there you drain the queue by tapping it, and without it the queue grows to its
    300 cap. That is why it was reported the day that switch existed rather than when this was
    written."""
    src = APP_JS
    fn = src[src.index("function onFeedScroll(){"):]
    fn = fn[:fn.index("\n  }")]
    assert "_flushPending()" in fn, "re-point this test — the flush moved out of the scroll handler"
    assert "document.hidden" in fn, \
        "the scroll handler flushes while the page is hidden — a backgrounded tab has no reader"
    assert "_cameBack" in fn, \
        "a scroll event fired while the view is being restored still counts as somebody scrolling, "\
        "so unlocking the phone throws the reading position away"


def test_coming_back_is_actually_recorded():
    """The guard above is only worth anything if something sets the timestamp."""
    assert "visibilitychange" in APP_JS and "_cameBack = Date.now()" in APP_JS
