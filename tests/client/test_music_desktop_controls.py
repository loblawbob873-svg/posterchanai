"""Two desktop music reports, one root: the desktop has NO floating player.

`html.os-on #music-player{display:none}` takes it out entirely, so on the windowed desktop the only
things that show or control music are the Music WINDOW and the Now-playing WIDGET. Both bugs are
code that still assumed the floating panel:

  * "I closed Music player now and I hear music still" — the CSS justified hiding the panel with
    "closing the Music window stops it", and os.js:closeWin deliberately does NOT stop playback
    (that is what once made the background player look dead). Nobody reconciled the two, so closing
    Music with no widget on the desk left audio running with no control anywhere on screen.
  * "widget on the desktop was not showing progress" — the once-a-second block that drives the
    widget AND the OS media session sat below `if(...contains('hidden')...) return`, and the panel
    is hidden always on the desktop.

The second is measured by running the shipped _tick (music_desktop_runtime.mjs); a source assertion
cannot see it, because the block is present and correct either way and only its POSITION decides
whether it runs.
"""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_the_desktop_widget_and_the_media_session_tick_with_the_panel_hidden():
    result = subprocess.run(['node', str(ROOT / 'tests/client/music_desktop_runtime.mjs')],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_premise_still_holds_that_the_desktop_hides_the_floating_player():
    """Everything below is reasoning about a desktop with no floating player. If that rule ever
    goes, these fixes are solving a problem that no longer exists and should be revisited rather
    than left in place."""
    assert "html.os-on #music-player{display:none}" in CSS


def _close_win():
    return OS_JS[OS_JS.index("  function closeWin(w, opts){"):OS_JS.index("  function minimise(w){")]


def test_closing_the_last_control_stops_the_music():
    body = _close_win()
    assert "opts.user" in body and "musicSurface()" in body
    guard = re.search(r"if\(opts && opts\.user && \(w\.view === 'doc:music' \|\| w\.view === '__music'\)"
                      r" && !musicSurface\(\)\)", body)
    assert guard, "the pause is not gated on all three of: a person, the music window, nothing left"


def test_every_human_close_says_so_and_nothing_else_does():
    """`user:true` is the whole gate, so the set of places that pass it is the contract.

    Miss one and that door silently stops working — Ctrl+W was missed on the first pass and this
    test is what found it. Add one to a path that is NOT a person and moving the Music window to
    the other monitor stops the music, because a handoff closes the source frame.
    """
    human = ["else if(a === 'close') closeWin(w, { user:true });",          # the title-bar button
             "{label:'Close', run:()=>closeWin(w, { user:true })}",         # the window menu
             "{label:'Close',run:()=>closeWin(running, { user:true })},",   # the taskbar menu
             "closeWin(f, { user:true }); return; }",                       # Ctrl+W / Alt+W
             "if(w) closeWin(w, { user:true });"]                            # Alt+F4 via pc:close
    for site in human:
        assert site in OS_JS, "a human close path stopped saying so: " + site
    assert OS_JS.count("user:true") == len(human), \
        "a call site gained or lost `user:true` — every one of them must be a PERSON, or a window " \
        "that merely moved monitors will stop the music"


def test_the_programmatic_closes_stay_silent():
    """Named, so that adding one of these to the human list is a decision somebody has to make."""
    for call in ("closeWin(w);", "closeWin(w, { killNative:", "closeWin(w,{preserveFocus:true});",
                 "closeWin(w,{killNative:false,preserveFocus:true});"):
        assert call in OS_JS, "expected a programmatic close site to still exist: " + call
    # popOut moves a window to its own OS frame, closeDoc is called by name, and a folder window
    # closes when its folder empties. None of the three is somebody saying "I am done listening".
    assert "try{ closeWin(w); }catch(_){ }" in OS_JS          # popOut
    assert "if(!w) return false;\n    closeWin(w);" in OS_JS   # closeDoc


def test_the_window_being_closed_cannot_answer_for_itself():
    """musicSurface() asks "is there a surface LEFT". closeWin splices the window out of `wins`
    first, so the check must run after that — otherwise the window being closed is itself the
    reason not to pause, and nothing ever stops."""
    body = _close_win()
    assert body.index("wins.splice(i, 1)") < body.index("!musicSurface()")


def test_a_widget_on_the_desk_is_a_control_and_playback_survives():
    """The pause exists for unreachable audio, not to end playback on principle: with a Now-playing
    widget mounted there is still a pause button, and a music app that stops when you close one of
    its windows is its own complaint."""
    fn = OS_JS[OS_JS.index("  function musicSurface(){"):]
    fn = fn[:fn.index("\n  }")]
    assert "m.w.type === 'music'" in fn, "a mounted Now-playing widget must count as a surface"
    assert "w.view === 'doc:music' || w.view === '__music'" in fn, \
        "another open Music window must count too"
    assert "if(!on) return true;" in fn, \
        "the classic UI still has the floating player and must never be touched by this"


def test_the_bridge_exposes_the_close_the_desktop_needs():
    assert "close: () => MusicPlayer.close()," in APP
    # close(), not pause(): there is no player left to leave paused, so the native media session and
    # its notification go with it — see MusicPlayer.close.
    assert "if(m && m.close) m.close();" in OS_JS
