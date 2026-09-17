"""Alt+Tab over a native application: the chooser must be SEEN, and Escape must cancel it.

Measured on the diagnostic two-output Wayfire session with Firefox and Telegram open (stipc keys,
grim of the output while Alt was held):

  * The chooser was built, focused and fullscreen, and invisible. `pc:wm:fullscreen` recognised a
    desktop surface only by the title "PosterChan" / "PosterChan · Nostr", but the shell's window is
    "PosterChan Desktop", so the gesture's failsafe was never registered and `sinkShellSurfaces`
    pushed the desktop straight back under Firefox. And on Wayfire neither fullscreen nor focusing
    the desktop raises it anyway, so the applications on that output are now sent under it.
  * Escape did nothing useful: with Alt still held the compositor saw Alt+Esc, which is
    fast-switcher's default binding, so focus jumped to another window and five seconds later the
    chooser's safety timer committed anyway.

The handler is RUN under node with the shipped source; the binding is read from the shipped config.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
INI = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(encoding="utf-8")


def _handler():
    start = MAIN.index("ipcMain.handle('pc:wm:fullscreen', ")
    end = MAIN.index("ipcMain.handle('pc:wm:snap'", start)
    body = MAIN[start:end].strip()
    body = body[len("ipcMain.handle('pc:wm:fullscreen', "):]
    assert body.endswith("});")
    return body[:-2]            # the `async (e, id, on) => {...}` function


def _run(scenario):
    script = """
const calls=[];let sunk=0;const timers=[];
const SHELL_MODE=true;
const _shellFullscreenFailsafes=new Map();
function fsGuard(){}
function setTimeout(fn,ms){timers.push(ms);return timers.length;}
function clearTimeout(){}
const S=%s;
function shellSurfaceIds(){return new Map(S.records.map(id=>[id,{conId:id}]));}
function sinkShellSurfaces(){sunk++;}
const W={
  fullscreen:async(id,on)=>{calls.push(['fullscreen',id,on]);return {ok:true};},
  windows:async()=>S.rows,
  keepBelow:async(id)=>{calls.push(['below',id]);return true;},
};
function wm(){return W;}
const handler=%s;
(async()=>{
  await handler({}, S.id, S.on);
  process.stdout.write(JSON.stringify({calls,sunk,timers,failsafe:_shellFullscreenFailsafes.has(S.id)}));
})();
""" % (json.dumps(scenario), _handler())
    return json.loads(subprocess.check_output(["node", "-e", script], text=True))


ROWS = [
    {"id": 5, "app": "place.poster.desktop", "title": "PosterChan Desktop", "outputName": "HEADLESS-2", "focusTime": 1},
    {"id": 7, "app": "place.poster.desktop", "title": "PosterChan Desktop", "outputName": "HEADLESS-1", "focusTime": 2},
    {"id": 9, "app": "firefox", "title": "Mozilla Firefox", "outputName": "HEADLESS-2", "focusTime": 30},
    {"id": 11, "app": "org.telegram.desktop", "title": "Telegram", "outputName": "HEADLESS-2", "focusTime": 50},
    {"id": 13, "app": "foot", "title": "foot", "outputName": "HEADLESS-1", "focusTime": 60},
    {"id": 15, "app": "mpv", "title": "hidden", "outputName": "HEADLESS-2", "focusTime": 70, "stashed": True},
]

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is required")


@needs_node
def test_the_desktop_going_fullscreen_for_alt_tab_is_kept_in_front():
    got = _run({"id": 5, "on": True, "records": [5, 7], "rows": ROWS})
    assert got["failsafe"], "the gesture's desktop was not recognised, so it is sunk mid-press"
    below = [c[1] for c in got["calls"] if c[0] == "below"]
    # Every visible application on THAT output goes under the desktop, most recently focused first so
    # their own order survives; the other output, the other desktop and a minimised app are untouched.
    assert below == [11, 9]
    assert got["timers"] and min(got["timers"]) >= 5000, "the failsafe must outlive the chooser (5s)"


@needs_node
def test_the_title_the_shell_really_has_is_recognised_without_a_record():
    got = _run({"id": 5, "on": True, "records": [], "rows": ROWS})
    assert got["failsafe"], "'PosterChan Desktop' is the shell's real title on Wayfire"


@needs_node
def test_a_surface_record_wins_over_whatever_the_title_says():
    """The records are the authority: a page can retitle its window, a record cannot drift."""
    rows = [dict(r, title="Messages - somewhere") if r["id"] == 5 else r for r in ROWS]
    got = _run({"id": 5, "on": True, "records": [5, 7], "rows": rows})
    assert got["failsafe"]


@needs_node
def test_an_application_going_fullscreen_moves_nothing():
    got = _run({"id": 9, "on": True, "records": [5, 7], "rows": ROWS})
    assert not got["failsafe"]
    assert not [c for c in got["calls"] if c[0] == "below"]


@needs_node
def test_ending_the_gesture_puts_the_desktop_back_under_the_applications():
    got = _run({"id": 5, "on": False, "records": [5, 7], "rows": ROWS})
    assert got["sunk"] == 1
    assert not [c for c in got["calls"] if c[0] == "below"]


def _section(name):
    m = re.search(r"^\[%s\]\n(.*?)(?=^\[|\Z)" % re.escape(name), INI, re.S | re.M)
    return m.group(1) if m else ""


def test_escape_during_alt_tab_reaches_the_chooser():
    """No binding may be Alt+Esc: that is exactly what cancelling Alt+Tab types."""
    bindings = {}
    for line in INI.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(<[^#]*KEY_[A-Z0-9_]+)\s*$", line)
        if m:
            bindings[m.group(1)] = " ".join(m.group(2).split())
    assert "<alt> KEY_ESC" not in bindings.values()
    fast = _section("fast-switcher")
    assert fast, "without its own section fast-switcher falls back to its default, Alt+Esc"
    act = re.search(r"^activate\s*=\s*(.+)$", fast, re.M)
    back = re.search(r"^activate_backward\s*=\s*(.+)$", fast, re.M)
    assert act and "<alt>" in act.group(1) and "<super>" in act.group(1)
    assert back and "<super>" in back.group(1)
    assert "fast-switcher" in re.search(r"^plugins\s*=.*$", INI, re.M).group(0), "the escape hatch stays"
