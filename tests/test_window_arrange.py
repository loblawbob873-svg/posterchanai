"""ARRANGE: every window on a monitor into a grid, side by side, or top and bottom.

Asked for: "window tiling support so we can do 4 windows in a grid", "an easy way to grid everything
on a desktop or split". Three layers, each run for real:

  * desktop/tile.js -- the arithmetic, for 1..6 windows: the rectangles never overlap and cover the
    work area exactly (adjacent windows meet, no seam and no overlap);
  * WayfireWM.arrange -- against a fake Wayfire socket that records what the compositor is told: only
    application windows on the chosen monitor move (not the desktop's own surfaces, popups, minimised
    or fullscreen windows, nor anything on the other monitor), most recently used first, inside the
    work area above the taskbar, with the title-bar allowance snap() uses;
  * the keys -- main.js arranges pc:arrange:<layout> ticks itself (once, not once per monitor).
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _node(src):
    r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def _tiles(layout, n, work=(0, 0, 1920, 1040)):
    return _node("const {tileRects}=require(%s);process.stdout.write(JSON.stringify(tileRects(%s,%d,{x:%d,y:%d,width:%d,height:%d})))"
                 % (json.dumps(str(ROOT / "desktop/tile.js")), json.dumps(layout), n, *work))


def _exact_cover(rects, work):
    x, y, w, h = work
    area = sum(r["w"] * r["h"] for r in rects)
    assert area == w * h, (area, w * h, rects)
    for i, a in enumerate(rects):
        assert a["x"] >= x and a["y"] >= y and a["x"] + a["w"] <= x + w and a["y"] + a["h"] <= y + h, a
        for b in rects[i + 1:]:
            overlap = (min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]) > 0 and
                       min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]) > 0)
            assert not overlap, (a, b)


def test_four_windows_make_a_two_by_two_grid():
    r = _tiles("grid", 4)
    assert r == [{"x": 0, "y": 0, "w": 960, "h": 520}, {"x": 960, "y": 0, "w": 960, "h": 520},
                 {"x": 0, "y": 520, "w": 960, "h": 520}, {"x": 960, "y": 520, "w": 960, "h": 520}], r


def test_every_count_and_layout_covers_the_area_exactly():
    work = (1920, 38, 1853, 1001)            # an odd-sized work area on a second monitor
    for layout in ("grid", "side-by-side", "stacked"):
        for n in range(1, 7):
            rects = _tiles(layout, n, work)
            assert len(rects) == n, (layout, n, rects)
            _exact_cover(rects, work)


def test_three_is_one_tall_and_two_stacked_and_splits_are_columns_or_rows():
    three = _tiles("grid", 3)
    assert three[0] == {"x": 0, "y": 0, "w": 960, "h": 1040}
    assert [r["x"] for r in three[1:]] == [960, 960] and [r["y"] for r in three[1:]] == [0, 520]
    assert [r["h"] for r in _tiles("side-by-side", 3)] == [1040] * 3
    assert [r["w"] for r in _tiles("stacked", 2)] == [1920] * 2
    assert _tiles("nonsense", 3) == [] and _tiles("grid", 0) == []


def test_the_bridge_arranges_only_the_application_windows_on_that_monitor(tmp_path):
    views = [
        # The desktop's own surface on monitor A: never moved.
        {"id": 1, "app-id": "posterchan-desktop", "title": "PosterChan Desktop", "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080}, "mapped": True, "output-id": 1, "last-focus-timestamp": 1},
        # Three apps on A, one of them a popped-out PosterChan window.
        {"id": 10, "app-id": "firefox", "title": "Firefox", "geometry": {"x": 100, "y": 100, "width": 800, "height": 600}, "mapped": True, "output-id": 1, "last-focus-timestamp": 30},
        {"id": 11, "app-id": "org.telegram.desktop", "title": "Telegram", "geometry": {"x": 300, "y": 200, "width": 700, "height": 500}, "mapped": True, "output-id": 1, "last-focus-timestamp": 50, "activated": True},
        {"id": 12, "app-id": "posterchan-desktop", "title": "PosterChan Window — mail", "geometry": {"x": 500, "y": 300, "width": 600, "height": 400}, "mapped": True, "output-id": 1, "last-focus-timestamp": 40},
        # Left alone: a popup, a minimised window, a fullscreen game, and a window on monitor B.
        {"id": 20, "app-id": "posterchan-desktop", "title": "PosterChan Popup", "geometry": {"x": 10, "y": 700, "width": 300, "height": 200}, "mapped": True, "output-id": 1},
        {"id": 21, "app-id": "gimp", "title": "GIMP", "geometry": {"x": 200, "y": 200, "width": 500, "height": 500}, "mapped": True, "output-id": 1, "minimized": True},
        {"id": 22, "app-id": "game", "title": "Game", "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080}, "mapped": True, "output-id": 1, "fullscreen": True},
        {"id": 30, "app-id": "obs", "title": "OBS", "geometry": {"x": 2100, "y": 100, "width": 900, "height": 700}, "mapped": True, "output-id": 2},
    ]
    outputs = [{"name": "DP-1", "id": 1, "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080}},
               {"name": "DP-2", "id": 2, "geometry": {"x": 1920, "y": 0, "width": 1920, "height": 1080}}]
    script = tmp_path / "arrange.js"
    script.write_text(textwrap.dedent(f"""
      const net=require('net'),fs=require('fs'),path=require('path'),os=require('os');
      const sock=path.join(fs.mkdtempSync(path.join(os.tmpdir(),'pcarr-')),'wf.sock'),calls=[];
      const frame=o=>{{const b=Buffer.from(JSON.stringify(o));const h=Buffer.alloc(4);h.writeUInt32LE(b.length);return Buffer.concat([h,b]);}};
      const views={json.dumps(views)}, outputs={json.dumps(outputs)};
      const server=net.createServer(c=>{{let b=Buffer.alloc(0);c.on('data',d=>{{b=Buffer.concat([b,d]);
        while(b.length>=4){{const n=b.readUInt32LE();if(b.length<4+n)return;const q=JSON.parse(b.subarray(4,4+n));b=b.subarray(4+n);calls.push(q);
          const r=q.method==='window-rules/list-views'?views:q.method==='window-rules/list-outputs'?outputs:{{result:'ok'}};c.write(frame(r));}}}});}});
      server.listen(sock,async()=>{{
        process.env.WAYFIRE_SOCKET=sock;
        const {{WayfireWM}}=require({json.dumps(str(ROOT / 'desktop/wm-wayfire.js'))});
        const w=new WayfireWM();
        await w.setWorkArea({{x:0,y:0,w:1920,h:1040}});      // the taskbar takes the bottom 40px
        const res=await w.arrange('grid',{{x:960,y:540}});
        const placed=calls.filter(q=>q.method==='window-rules/configure-view').map(q=>({{id:q.data.id,g:q.data.geometry,out:q.data.output_id}}));
        const bad=await w.arrange('spiral',{{x:960,y:540}});
        console.log(JSON.stringify({{res,placed,bad}}));
        try{{w.sock.destroy();}}catch(_){{}} server.close();process.exit(0);
      }});
    """), encoding="utf-8")
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout)
    assert got["res"] == {"ok": True, "layout": "grid", "output": "DP-1", "count": 3}, got
    ids = [p["id"] for p in got["placed"]]
    assert ids == [11, 12, 10], ("most recently used first, and only the three apps on this monitor", got["placed"])
    g = {p["id"]: p["g"] for p in got["placed"]}
    assert g[11] == {"x": 0, "y": 0, "width": 960, "height": 1040}, g          # one tall on the left
    assert g[12]["x"] == 960 and g[10]["y"] + g[10]["height"] <= 1040, g        # nothing under the taskbar
    assert got["bad"]["ok"] is False


def test_the_keys_are_arranged_once_in_main_and_bound_in_wayfire():
    main = (ROOT / "desktop/main.js").read_text()
    tick = main[main.index("wm().on('tick', (ev) => {"):][:1600]
    assert "ARRANGE_TICKS.includes(" in tick and "arrange(String(ev.payload).slice(11), 'focused')" in tick
    listed = main[main.index("const ARRANGE_TICKS = ["):].split("]", 1)[0]
    for layout in ("grid", "side-by-side", "stacked"):
        assert f"'pc:arrange:{layout}'" in listed, layout
    ini = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text()
    for key, layout in (("<super> KEY_G", "grid"), ("<super> <shift> KEY_G", "side-by-side"), ("<super> <alt> KEY_G", "stacked")):
        assert key in ini and "pc:arrange:" + layout in ini, (key, layout)
    assert "arrange: (layout) => ipcRenderer.invoke('pc:wm:arrange'" in (ROOT / "desktop/preload.js").read_text()
