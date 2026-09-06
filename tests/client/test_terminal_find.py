"""Terminal scrollback search must stay reachable and local to xterm."""
from pathlib import Path
import json
import re
import shutil
import subprocess


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
    assert out.index("scrollingByUs=true") < out.index("term.write(output")
    assert "const followThisWrite=followBottom" in out


def test_reset_cannot_turn_off_follow_mode_before_mobile_replay():
    reset = TERM[TERM.index("function _resetForReplay"):
                 TERM.index("async function connect", TERM.index("function _resetForReplay"))]
    assert reset.index("followBottom = true") < reset.index("term.reset()")
    assert reset.index("scrollingByUs = true") < reset.index("term.reset()")
    assert "_pinBottomAfterLayout()" in reset
    connect = TERM[TERM.index("async function connect"):TERM.index("function attach")]
    attach = TERM[TERM.index("function attach"):TERM.index("function _cycleTab")]
    assert "_resetForReplay()" in connect
    assert "_resetForReplay()" in attach


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
    assert "_scrollsAway('wheel',ev.deltaY)" in mount
    assert "addEventListener('touchmove'" in mount
    assert "_scrollsAway('touchmove')" in mount
    assert "addEventListener('pointerdown'" not in mount

    keys = TERM[TERM.index("attachCustomKeyEventHandler"):
                TERM.index("return true;", TERM.index("attachCustomKeyEventHandler"))]
    assert "ev.key === 'PageUp'" in keys
    assert "_stopFollowing()" in keys


def test_tap_to_focus_is_not_runtime_scroll_intent():
    node = shutil.which("node")
    if not node:
        return
    match = re.search(r"function _scrollsAway\(kind, delta\)\{[\s\S]*?\n    \}", TERM)
    assert match, "the shipped scroll-intent decision is missing"
    js = match.group(0) + "\nconsole.log(JSON.stringify([" + \
         "_scrollsAway('pointerdown'),_scrollsAway('touchmove')," + \
         "_scrollsAway('wheel',-1),_scrollsAway('wheel',1)]));"
    got = subprocess.run([node, "-e", js], capture_output=True, text=True, check=True)
    assert json.loads(got.stdout) == [False, True, True, False]


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


def test_resize_keeps_a_live_terminal_at_the_prompt_without_fighting_scrollback():
    fit = TERM[TERM.index("function _fit()"):
               TERM.index("/* ONE WAY OUT", TERM.index("function _fit()"))]
    assert "const followThisFit=followBottom" in fit
    assert fit.index("if(followThisFit)scrollingByUs=true") < fit.index("if(fit){ fit.fit(); fitOk=true; }")
    assert "if(followThisFit)_pinBottomAfterLayout()" in fit


def test_reconnect_preserves_scrollback_but_initial_attach_opens_at_current_output():
    """READY is shared by fresh opens, explicit attaches and automatic reconnects. Only the first
    two reset replay state; making READY itself enable following destroys a deliberate scroll-up
    whenever Android wakes and reconnects its socket."""
    ready = TERM[TERM.index("if(m.t === 'ready')"):TERM.index("if(m.t === 'gone')")]
    assert "followBottom=true" not in ready
    assert "if(followBottom) _pinBottomAfterLayout()" in ready

    reset = TERM[TERM.index("function _resetForReplay"):
                 TERM.index("async function connect", TERM.index("function _resetForReplay"))]
    assert "followBottom = true" in reset
    connect = TERM[TERM.index("async function connect"):TERM.index("function attach")]
    attach = TERM[TERM.index("function attach"):TERM.index("function _cycleTab")]
    assert "_resetForReplay()" in connect
    assert "_resetForReplay()" in attach

    later = TERM[TERM.index("function _later()"):
                 TERM.index("function _wake()", TERM.index("function _later()"))]
    assert "_open({ resume: sid, host, label })" in later
    assert "_resetForReplay()" not in later


def test_resize_guards_measure_the_live_terminal_element_not_an_out_of_scope_local():
    """The mount function's `const box` is not visible in sibling `_fit`; caught ReferenceErrors
    used to turn every focus/geometry guard into a silent no-op."""
    fit = TERM[TERM.index("function _fit()"):
               TERM.index("/* ONE WAY OUT", TERM.index("function _fit()"))]
    resolve = "const box = $('#tty-screen')"
    assert resolve in fit
    assert fit.index(resolve) < fit.index("box.closest")
    assert fit.index(resolve) < fit.index("box.getBoundingClientRect")
    assert "if(!box || !box.isConnected) return" in fit


def test_focus_return_retries_a_fit_that_ran_before_xterms_viewport_was_ready():
    """The same ResizeObserver rectangle must not be deduplicated after FitAddon threw once."""
    fit = TERM[TERM.index("function _fit()"):
               TERM.index("/* ONE WAY OUT", TERM.index("function _fit()"))]
    assert "let fitOk=!fit" in fit
    assert "if(fit){ fit.fit(); fitOk=true; }" in fit
    assert "if(px&&fitOk)_fitPixels=px" in fit
    assert "if(px)_fitPixels=px" not in fit


def test_ctrl_page_keys_cycle_terminal_tabs_and_wrap_locally():
    assert "function _cycleTab(step)" in TERM
    assert "(at+(step<0?-1:1)+tabs.length)%tabs.length" in TERM
    chord = TERM[TERM.index("function _tabChord(ev)"):
                 TERM.index("async function _sessions(", TERM.index("function _tabChord(ev)"))]
    assert "ev.key!=='PageUp'&&ev.key!=='PageDown'" in chord
    assert "_cycleTab(ev.key==='PageUp'?-1:1)" in chord
    keys = TERM[TERM.index("attachCustomKeyEventHandler"):
                TERM.index("return true;", TERM.index("attachCustomKeyEventHandler"))]
    assert "if(_tabChord(ev))" in keys
    assert "return false" in keys


def test_find_navigation_and_close_are_wired():
    assert "_findMove(ev.shiftKey ? -1 : 1)" in TERM
    assert "if(ev.key === 'Escape')" in TERM
    assert ".tty-find[hidden]{display:none}" in CSS
