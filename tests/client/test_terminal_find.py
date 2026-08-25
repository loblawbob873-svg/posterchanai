"""Terminal scrollback search must stay reachable and local to xterm."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TERM = (ROOT / "static/js/client/term.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_find_has_visible_controls_and_keyboard_shortcut():
    for marker in ('id="tty-find"', 'id="tty-find-input"', 'id="tty-find-prev"',
                   'id="tty-find-next"', 'id="tty-find-close"'):
        assert marker in TERM
    assert "attachCustomKeyEventHandler" in TERM
    assert "ev.ctrlKey || ev.metaKey" in TERM
    assert "ev.shiftKey" in TERM


def test_find_searches_xterm_scrollback_and_selects_the_match():
    assert "term.buffer.active" in TERM
    assert "b.getLine(row)" in TERM
    assert "translateToString(true)" in TERM
    assert "term.select(h.col, h.row, h.len)" in TERM
    assert "term.scrollToLine(h.row)" in TERM


def test_attach_starts_at_live_prompt_then_respects_manual_scrolling():
    assert "term.onScroll" in TERM
    assert "followBottom=!!b && y>=b.baseY" in TERM
    assert "followBottom = true" in TERM
    assert "term.scrollToBottom()" in TERM
    # xterm emits onScroll while replay itself grows the buffer. The programmatic-scroll guard must
    # be armed before term.write, or that event disables follow mode before its callback can land at
    # the prompt.
    out = TERM[TERM.index("if(m.t === 'out')"):TERM.index("if(m.t === 'ready')")]
    assert out.index("scrollingByUs=true") < out.index("term.write(m.d")
    assert "const followThisWrite=followBottom" in out


def test_user_scrolling_cancels_an_inflight_bottom_pin():
    """Live output must not make the terminal impossible to scroll while a pin timer is armed."""
    stop = TERM[TERM.index("function _stopFollowing"):
                TERM.index("function _pinBottomAfterLayout")]
    assert "followBottom = false" in stop
    assert "scrollingByUs = false" in stop
    assert "++bottomPinEpoch" in stop
    assert "clearTimeout(bottomPinT)" in stop

    mount = TERM[TERM.index("function _mountTerm"):
                 TERM.index("/* FIND LIVES IN THE RENDERER")]
    assert "addEventListener('wheel'" in mount
    assert "Number(ev.deltaY) < 0" in mount
    assert "addEventListener('touchmove', _stopFollowing" in mount
    assert "closest('.xterm-viewport')" in mount

    keys = TERM[TERM.index("attachCustomKeyEventHandler"):
                TERM.index("return true;", TERM.index("attachCustomKeyEventHandler"))]
    assert "ev.key === 'PageUp'" in keys
    assert "_stopFollowing()" in keys


def test_large_replay_stays_pinned_until_chromium_finishes_layout():
    """A write callback can precede the final scrollHeight in packaged Electron."""
    pin = TERM[TERM.index("function _pinBottomAfterLayout"):
               TERM.index("function pageZoom")]
    assert "const mine = ++bottomPinEpoch" in pin
    assert "requestAnimationFrame(() => requestAnimationFrame(settle))" in pin
    assert "setTimeout(settle, 120)" in pin
    assert "if(mine !== bottomPinEpoch" in pin
    assert pin.count("term.scrollToBottom()") >= 2

    out = TERM[TERM.index("if(m.t === 'out')"):TERM.index("if(m.t === 'ready')")]
    assert "if(followThisWrite) _pinBottomAfterLayout()" in out


def test_find_navigation_and_close_are_wired():
    assert "_findMove(ev.shiftKey ? -1 : 1)" in TERM
    assert "if(ev.key === 'Escape')" in TERM
    assert ".tty-find[hidden]{display:none}" in CSS
