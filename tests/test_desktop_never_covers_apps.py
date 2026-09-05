"""THE DESKTOP IS NOT AN APPLICATION, AND ON WAYFIRE IT WAS STACKED LIKE ONE.

Reported in one burst: "opening a new window hides all the other windows on the desktop!", "volume
widget hides every open window", "same for notifications", "resizing windows with super + click,
hides it right after resize", and "alt_tab are all blank previews". Five reports, one fact.

MEASURED on the installed machine (192.168.0.102, Wayfire 0.10.1, two 3072x2048 outputs at scale
1.25), with `grim` as the honest channel:

    window-rules/focus-view 74   (OBS)   -> the photograph of DP-1 shows OBS above the desktop
    window-rules/focus-view 400  (shell) -> the same photograph shows the bare desktop, and OBS,
                                            LibreOffice and four popped-out PosterChan windows are
                                            simply not in it

Nothing was minimised, nothing moved, nothing appeared in any log. Sway had TWO stacks and painted
floating over tiled unconditionally, so the shell — one opaque toplevel filling the output — could
never rise above an application; that assumption is written into os.js, osnative.js and main.js in
so many words ("a PosterChan window can never be drawn in front of Firefox or Telegram"). Wayfire
has one workspace layer ordered by focus, so every click on the taskbar, on a desktop icon or on the
tray chip covers the whole monitor.

And it is why every Alt+Tab card was blank: the capture path photographs a SCREEN RECTANGLE, and the
screen at those coordinates was the desktop.

`wm-actions/send-to-back` is the lever, and it is ONE-SHOT — measured, a single later focus put the
shell straight back on top — which is why the fix is an event handler rather than a state set once.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import struct
import subprocess
import textwrap
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN_JS = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
WAYFIRE_INI = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(encoding="utf-8")


def _png(width: int, height: int) -> bytes:
    """The smallest real PNG — view-shot writes a file and the backend must accept only a PNG."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_wayfire_backend_can_put_the_desktop_under_the_applications(tmp_path):
    """RUN the real backend against a real Wayfire wire protocol. `keepBelow` has to reach
    `wm-actions/send-to-back` with the `view_id`/`state` shape the compositor answered on the
    machine — `{"view_id": N}` without `state` was refused with `Missing "state"`, and `{"id": N}`
    with `Missing "view_id"`, so the field names are load-bearing rather than cosmetic."""
    sock = tmp_path / "wayfire-wayland-9.socket"
    script = tmp_path / "keep-below.js"
    script.write_text(textwrap.dedent(f"""
      const net=require('net'),fs=require('fs');
      const sock={json.dumps(str(sock))};
      const calls=[];
      const frame=o=>{{const b=Buffer.from(JSON.stringify(o)),h=Buffer.alloc(4);h.writeUInt32LE(b.length);return Buffer.concat([h,b]);}};
      const server=net.createServer(c=>{{let b=Buffer.alloc(0);c.on('data',x=>{{b=Buffer.concat([b,x]);
        while(b.length>=4){{const n=b.readUInt32LE();if(b.length<4+n)return;
          const q=JSON.parse(b.subarray(4,4+n));b=b.subarray(4+n);calls.push(q);
          if(q.method==='view-shot/capture')fs.writeFileSync(q.data.file,
            Buffer.from({json.dumps(base64.b64encode(_png(4, 3)).decode())},'base64'));
          c.write(frame({{result:'ok'}}));}}}});}});
      server.listen(sock,async()=>{{
        process.env.WAYFIRE_SOCKET=sock;
        const {{WayfireWM}}=require({json.dumps(str(ROOT / 'desktop/wm-wayfire.js'))});
        const w=new WayfireWM();
        await w.keepBelow(400,true);
        const shot=await w.captureView(400);
        const missing=await w.captureView(0/0);
        // The SWAY class by name. `WM` is the FACTORY and picks Wayfire whenever WAYFIRE_SOCKET is
        // set, which it is here — asking it would test this backend twice and prove nothing.
        const {{SwayWM}}=require({json.dumps(str(ROOT / 'desktop/wm.js'))});
        console.log(JSON.stringify({{calls,shot:String(shot).slice(0,22),missing,
          sway:await new SwayWM('/nonexistent').keepBelow(1)}}));
        w.sock.destroy();server.close();process.exit(0);
      }});
    """), encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)

    sink = [c for c in got["calls"] if c["method"] == "wm-actions/send-to-back"]
    assert sink == [{"method": "wm-actions/send-to-back",
                     "data": {"view_id": 400, "state": True}}], got["calls"]

    # A PICTURE OF THE VIEW, NOT OF THE SCREEN. Measured on the machine against a window sitting
    # under BOTH the desktop shell and a fullscreen game: view-shot returned a 1942x1529 PNG of that
    # window's real contents, where grim of the same rectangle returns wallpaper.
    capture = [c for c in got["calls"] if c["method"] == "view-shot/capture"]
    assert capture and capture[0]["data"]["view-id"] == 400
    assert capture[0]["data"]["file"].endswith(".png")
    assert got["shot"].startswith("data:image/png;base64,")
    # An absent plugin or a bad id costs the preview, never the desktop: main falls back to grim.
    assert got["missing"] == ""
    # Sway needs no lever and must not pretend to have one — it already paints the shell underneath.
    assert got["sway"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_new_window_opens_at_the_size_the_desktop_chose_for_it(tmp_path):
    """REPORT 5 — "its not opening windows at a decent size, we fixed this before on the old sway".

    MEASURED live: `PosterChan Window — bookmarks` and `PosterChan Window — terminal` were both
    exactly 1100x760 on a 3072x2048 output, which is oswin.js's fallback, while the two windows that
    had been popped OUT of an in-page frame — the one path that passes a size — were 1910x1487 and
    1983x1831. `openApp` asked for a toplevel with `{}`, so `place()` (the whole measure-the-desk,
    shape-per-app decision) reached nothing on the machine it was written for.

    This RUNS the shipped conversion, because the numbers are the point: three coordinate systems
    (layout px, the zoomed page, compositor units) and an answer that must not silently become the
    fallback again."""
    script = tmp_path / "open-size.js"
    script.write_text(textwrap.dedent(f"""
      const NAT=require({json.dumps(str(ROOT / 'static/js/client/osnative.js'))});
      // `place()` for a 'wide' app on the measured desk, in LAYOUT pixels, and the shell's own
      // window measured in both systems at once exactly as scaleFrom() derives it.
      const shell={{x:0,y:0,width:3072,height:2048}};
      const scale=NAT.scaleFrom(shell,3072,2048);
      const half=NAT.scaleFrom(shell,3840,2560);            // the same desk read at 1.25x
      console.log(JSON.stringify({{
        wide:NAT.windowOpenSize({{w:2520,h:1900}},1,scale),
        scaled:NAT.windowOpenSize({{w:2520,h:1900}},1,half),
        zoomed:NAT.windowOpenSize({{w:1260,h:950}},2,scale),
        nothing:NAT.windowOpenSize({{w:0,h:0}},1,scale),
        silly:NAT.windowOpenSize({{w:4,h:3}},1,scale),
        noScale:NAT.windowOpenSize({{w:2520,h:1900}},1,null)}}));
    """), encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)

    assert got["wide"] == {"width": 2520, "height": 1900}
    # The renderer measuring itself at 1.25x is the same desk; the answer converts rather than
    # doubling, which is the trap `mapRect` already carries a comment about.
    assert got["scaled"] == {"width": 2016, "height": 1520}
    assert got["zoomed"] == {"width": 2520, "height": 1900}
    # A size that could not be worked out must fall through to oswin.js's own default, never to a
    # window a few pixels across.
    assert got["nothing"] is None and got["silly"] is None
    # No shell rectangle yet (nothing native has ever been hosted, so nsync never recorded one) is
    # the ordinary state of this machine: fall back to the page's own pixels, which is what popOut
    # has always passed — never to the 1100x760 fallback.
    assert got["noScale"] == {"width": 2520, "height": 1900}


def test_open_app_hands_that_size_to_the_window_it_opens():
    """The conversion above is worth nothing if the call site still passes `{}` — which is the whole
    bug, and it is one character wide."""
    call = re.search(r"real = PCOSWin\.open\(view, label \|\| view, ([^)]*)\)", OS_JS)
    assert call, "openApp no longer opens a toplevel through PCOSWin.open — re-read this test"
    assert call.group(1).strip() != "{}", (
        "openApp is asking for a window with no size again: every app on PosterChanOS then opens at "
        "oswin.js's 1100x760 fallback and place() reaches nothing")
    assert "_windowOpenHint" in call.group(1)
    assert "function _windowOpenHint" in OS_JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_stacking_plan_would_minimise_the_desktop_itself(tmp_path):
    """`domStackPlan` skips `row.own` — and NOTHING SET IT. The rows handed to it come from
    `pcWM.snapshot()`, i.e. the compositor's own view records, which carry no such field; only
    `taskbarRows` adds it, and that is a different list. So its one protection against putting away
    OUR surfaces was inert, and the two it would put away are the shell (a full-output window
    containing the very frame being raised) and every popped-out PosterChan window, which has no
    HTML frame left to bring it back with.

    RUN the shipped plan against the shape the snapshot really produces."""
    script = tmp_path / "dom-stack.js"
    script.write_text(textwrap.dedent(f"""
      const NAT=require({json.dumps(str(ROOT / 'static/js/client/osnative.js'))});
      // Exactly what `pc:wm:snapshot` answers: normalizeView rows, with no `own` anywhere.
      const rows=[{{id:400,app:'place.poster.desktop',rect:{{x:0,y:0,width:3072,height:2048}}}},
                  {{id:505,app:'place.poster.desktop',rect:{{x:900,y:400,width:1100,height:760}}}},
                  {{id:74,app:'com.obsproject.Studio',rect:{{x:1035,y:228,width:1565,height:1596}}}}];
      const frame={{left:1000,top:300,width:1300,height:1000}};
      const marked=rows.map(r=>/^place\\.poster\\.desktop$/.test(r.app)?Object.assign({{}},r,{{own:true}}):r);
      console.log(JSON.stringify({{raw:NAT.domStackPlan(rows,frame).hide,
                                  marked:NAT.domStackPlan(marked,frame).hide}}));
    """), encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert 400 in got["raw"] and 505 in got["raw"], (
        "re-read this test: the plan no longer treats an unmarked shell surface as something to hide")
    assert got["marked"] == [74], got["marked"]
    # …and the caller must be the thing that marks them.
    assert "Object.assign({},r,{own:true})" in OS_JS.replace(" ", ""), (
        "_stackDomAboveNative is handing raw snapshot rows to domStackPlan again, so it can minimise "
        "the desktop and every popped-out PosterChan window")


def test_the_shell_surface_is_sent_back_whenever_it_is_focused():
    """The handler, and the one exception. Source-level because it lives in Electron's main process,
    but every fact it asserts is a fact the machine measured."""
    assert "wm().on('window', sinkShellOnFocus)" in MAIN_JS, (
        "nothing sinks the desktop any more: focusing it covers every application on that monitor")
    handler = MAIN_JS[MAIN_JS.index("function sinkShellSurfaces()"):
                      MAIN_JS.index("async function wireShellRecovery()")]
    assert "keepBelow" in handler
    # ONE-SHOT, measured: send-to-back followed by a focus put the shell straight back on top. So it
    # has to run on the EVENT; a single call at assignment time would last exactly one focus.
    assert "view-focused" in handler and "view-mapped" in handler
    # The exception, and the only one: a window this shell DRAWS (Settings, Task Manager, VMs,
    # Remote Desktop, a folder) is focused and has no toplevel of its own to raise instead.
    assert "_shellWantsFront" in handler
    # …and Alt+Tab, which says it is deliberately in front by going FULLSCREEN — the state that
    # outranks everything, and the only reason its chooser can be seen at all. Its own gesture emits
    # focus events, so without this exemption the surface would be pushed back under the
    # applications with the chooser drawn on it, mid-press.
    assert "_shellFullscreenFailsafes.has(id)" in handler
    assert "ipcMain.handle('pc:wm:shell-front'" in MAIN_JS
    assert "_shellWantsFront.delete(contentsId)" in MAIN_JS, (
        "a closed surface would keep the desktop entitled to sit above every application for ever")
    assert "shellFront:" in PRELOAD, "the renderer has no way to ask"
    assert "_publishShellFront" in OS_JS and "function drawBar(){\n    _publishShellFront();" in OS_JS
    # …and leaving the desktop hands the surface back: in Classic there is no taskbar and no
    # desktop, so this window is just PosterChan and an application that cannot be raised above
    # Firefox is not one. Said at the moment it changes, because no draw follows an exit.
    front = OS_JS[OS_JS.index("function _publishShellFront()"):OS_JS.index("function drawBar()")]
    assert "want = !on ||" in front, "the rule would keep the classic client under every application"
    assert "on = false;\n    _publishShellFront();" in OS_JS
    # …and the entitlement is dropped the moment somebody else holds the keyboard. A Settings frame
    # keeps its `focused` class until it is closed — clicking Firefox is not something this renderer
    # ever sees — so without this one open utility window would entitle the desktop to cover the
    # monitor on every later taskbar click, which is the original report all over again.
    assert "!_foreignFocused" in front
    assert "list.find(x => x && x.focused && Number(x.id) !== shellId)" in OS_JS


def test_alt_tab_photographs_the_window_and_not_the_screen_behind_it():
    """REPORT 8. The capture path refused a stashed or overlapped window and otherwise photographed
    a screen rectangle — and with the desktop above everything, the screen at those coordinates IS
    the desktop. Order is the property: a view capture that is tried AFTER the refusals is a view
    capture that never runs."""
    handler = MAIN_JS[MAIN_JS.index("ipcMain.handle('pc:wm:preview'"):
                      MAIN_JS.index("require('./native-preview.js').capture")]
    assert "captureView" in handler, "the preview is back to photographing the screen"
    assert handler.index("captureView") < handler.index("target.stashed"), (
        "the per-view capture is tried after the refusals that exist only because grim photographs "
        "the screen, so it can never run for the windows it was added for")
    # …and the compositor has to be told to load the plugin that provides it.
    plugins = next(line for line in WAYFIRE_INI.splitlines() if line.startswith("plugins ="))
    assert " view-shot" in plugins, plugins


def test_firefox_is_told_to_draw_the_frame_the_compositor_cannot_give_it():
    """REPORTS 6 and 7, which are one fact seen from both ends. `preferred_decoration_mode = server`
    only reaches clients that speak zxdg_decoration_manager_v1: Qt does, so OBS gets a Wayfire title
    bar; GTK does not, and MEASURED on the machine there is not one occurrence of that interface
    name in Firefox's libxul (`strings /opt/firefox/libxul.so | grep -c zxdg_decoration_manager_v1`
    -> 0). No compositor setting, per-app rule or window rule can put a frame on Firefox. So the
    browser is asked to draw its own instead of putting its tab strip where a title bar goes."""
    policy = ROOT / "os/overlay/app-misc/posterchanos-shell/files/firefox-policies.json"
    assert policy.exists(), "the policy is gone, so Firefox is back to having no title bar"
    doc = json.loads(policy.read_text(encoding="utf-8"))
    pref = doc["policies"]["Preferences"]["browser.tabs.inTitlebar"]
    assert pref["Value"] == 0
    # NOT locked: this is a default the person may still change on their own machine.
    assert pref["Status"] == "default"
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/"
              "posterchanos-shell-1.0.0.ebuild").read_text(encoding="utf-8")
    assert "/etc/firefox/policies" in ebuild and "firefox-policies.json" in ebuild
    # …and the direct/LiveCD installer, for the same reason /etc/wayfire.ini is copied there: the
    # overlay emerge is a `|| true` and may not have run.
    gentoo = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")
    assert "firefox-policies.json" in gentoo
