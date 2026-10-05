"""Arrange -> Grid on PosterChanOS, replayed against what the compositor answered on the real desk.

"tiling grid on desktop is ass and not working at all". tests/fixtures/wayfire_desk_arrange.json is
Wayfire 0.10.1's list-outputs/list-views from the two-monitor desk right after the user tried it. On
it the shipped arrange() produced: a 42px gap between the rows and a 10px gap at the top (a window's
invisible drop shadow reserved as if it were a title bar), columns overlapping (the shadow's width
never counted), and Messages left in the quarter Wayfire's own grid had snapped it to (configure-view
does not move a grid-tiled window). Runs the SHIPPED desktop/wm-wayfire.js under node against a fake
compositor that answers with the recorded data and applies every configure-view as Wayfire does.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures/wayfire_desk_arrange.json"

SCRIPT = r"""
const { WayfireWM } = require(%(wm)s);
const raw = JSON.parse(require('fs').readFileSync(%(fix)s, 'utf8'));
const extra = %(extra)s;
const views = JSON.parse(JSON.stringify(raw.views)).concat(extra);
for(const id of %(minimize)s){ const v = views.find(x => x.id === id); if(v) v.minimized = true; }
const wm = new WayfireWM('/nonexistent'), log = [];
wm.arrangeSettleMs = 0;
const revert = %(revert)s;      // {id: geometry}: something puts this window back once, after it is placed
let reverted = false;
wm._send = async (method, data) => {
  if(method === 'window-rules/list-outputs') return raw.outputs;
  if(method === 'window-rules/list-views'){
    if(revert && !reverted && log.some(([m, d]) => m === 'window-rules/configure-view' && d && d.geometry && String(d.id) in revert)){
      reverted = true;
      for(const [id, g] of Object.entries(revert)){ const v = views.find(x => x.id === Number(id)); if(v) v.geometry = Object.assign({}, g); }
    }
    return views;
  }
  log.push([method, data]);
  if(method === 'wm-actions/set-minimized'){ const v = views.find(x => x.id === data.view_id); if(v) v.minimized = !!data.state; }
  if(method === 'grid/restore'){ const v = views.find(x => x.id === (data.view_id ?? data['view-id'])); if(v) v['tiled-edges'] = 0; }
  if(method === 'window-rules/configure-view' && data.geometry){
    const v = views.find(x => x.id === data.id);
    // As Wayfire: a grid-tiled window keeps its slot.
    if(v && !(Number(v['tiled-edges']) > 0)) v.geometry = Object.assign({}, data.geometry);
  }
  return { result: 'ok' };
};
(async () => {
  await wm.setWorkArea(%(work)s);
  const r = await wm.arrange('grid', %(where)s);
  await wm._send('window-rules/list-views');   // what the compositor holds NOW (a pull-back lands here)
  const out = views.filter(v => v.mapped && v.role === 'toplevel' && /^PosterChan Window|^Firefox/.test(v.title))
    .map(v => ({ id: v.id, title: v.title, out: v['output-name'], g: v.geometry, tiled: v['tiled-edges'], min: !!v.minimized }));
  console.log(JSON.stringify({ r, log, out }));
})().catch(e => { console.error(e); process.exit(1); });
"""

DP1_WORK = {"x": 0, "y": 10, "w": 3840, "h": 2488}       # DP-1 sits at y=10; the taskbar takes 72


def _arrange(where, extra=None, work=DP1_WORK, revert=None, minimize=()):
    script = SCRIPT % {"revert": json.dumps(revert or {}), "minimize": json.dumps(list(minimize)),"wm": json.dumps(str(ROOT / "desktop/wm-wayfire.js")), "fix": json.dumps(str(FIX)),
                       "extra": json.dumps(extra or []), "work": json.dumps(work), "where": json.dumps(where)}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_four_windows_tile_the_monitor_exactly():
    d = _arrange({"x": 1920, "y": 1290})
    assert d["r"]["ok"] and d["r"]["output"] == "DP-1" and d["r"]["count"] == 4, d["r"]
    rects = [w["g"] for w in d["out"] if w["out"] == "DP-1"]
    assert len(rects) == 4, d["out"]
    # Output-local: DP-1's work area is 3840 x 2488 from its own top.
    area = 3840 * 2488
    assert sum(r["width"] * r["height"] for r in rects) == area, ("gaps or overlaps between the windows", rects)
    for a in rects:
        assert 0 <= a["x"] and a["x"] + a["width"] <= 3840 and 0 <= a["y"] and a["y"] + a["height"] <= 2488, rects
        for b in rects:
            if a is not b:
                ox = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
                oy = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
                assert ox <= 0 or oy <= 0, ("two windows overlap", a, b)


def test_a_window_wayfires_grid_had_snapped_is_released_and_moved():
    d = _arrange({"x": 1920, "y": 1290})
    msgs = next(w for w in d["out"] if w["title"].endswith("messages"))
    restores = [i for i, (m, _) in enumerate(d["log"]) if m == "grid/restore"]
    configures = [i for i, (m, data) in enumerate(d["log"]) if m == "window-rules/configure-view" and data.get("id") == msgs["id"]]
    assert restores and configures and restores[0] < configures[0], ("the snapped window was not released first", d["log"])
    assert msgs["tiled"] == 0 and msgs["g"]["height"] == 1244, ("Messages stayed in Wayfire's quarter", msgs)


def test_the_other_monitor_arranges_only_its_own_window():
    d = _arrange({"x": 5760, "y": 1290}, work={"x": 3840, "y": 0, "w": 3840, "h": 2488})
    assert d["r"]["output"] == "DP-2" and d["r"]["count"] == 1, d["r"]
    term = next(w for w in d["out"] if w["title"].endswith("terminal"))
    assert term["g"] == {"x": 0, "y": 0, "width": 3840, "height": 2488}, term


def test_a_real_title_bar_still_gets_its_room():
    """A server-decorated window (Firefox: 3px border, 23px title bar above, 29 below -- measured) must
    keep its title bar on screen; only invisible shadows stop reserving space."""
    ff = {"id": 99, "app-id": "firefox", "title": "Firefox", "mapped": True, "role": "toplevel", "tiled-edges": 0,
          "output-id": 3, "output-name": "DP-2", "last-focus-timestamp": 1, "layer": "workspace", "type": "toplevel",
          "geometry": {"x": 600, "y": 300, "width": 1000, "height": 800},
          "base-geometry": {"x": 597, "y": 277, "width": 1006, "height": 852}}
    d = _arrange({"x": 5760, "y": 1290}, extra=[ff], work={"x": 3840, "y": 0, "w": 3840, "h": 2488})
    f = next(w for w in d["out"] if w["title"] == "Firefox")
    assert f["g"]["y"] >= 23, ("the title bar was pushed off the top", f)


def test_the_bridge_asks_the_compositor_to_let_a_window_pass_clicks_through():
    """The dancer's "Let clicks through" on PosterChanOS: Electron cannot do it on Wayland (measured), so
    the bridge asks the posterchan-shell plugin, and reports a plugin that is too old as false."""
    script = r"""
const { WayfireWM } = require(%s);
const wm = new WayfireWM('/nonexistent'), sent = [];
let answer = { result: 'ok' };
wm._send = async (m, d) => { sent.push([m, d]); if(answer instanceof Error) throw answer; return answer; };
(async () => {
  const on = await wm.inputPassthrough(42, true), off = await wm.inputPassthrough('42', 0);
  answer = new Error('No such method found!'); const old = await wm.inputPassthrough(42, true);
  console.log(JSON.stringify({ sent, on, off, old }));
})();
""" % json.dumps(str(ROOT / "desktop/wm-wayfire.js"))
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["sent"][0] == ["posterchan-shell/input-passthrough", {"id": 42, "on": True}], d
    assert d["sent"][1] == ["posterchan-shell/input-passthrough", {"id": 42, "on": False}], d
    assert d["on"] is True and d["off"] is True and d["old"] is False, d


def _dp1(d):
    return [w for w in d["out"] if w["out"] == "DP-1"]


def test_a_minimized_window_is_shown_and_arranged():
    """'if a window is minimized, it won't grid'."""
    first = _dp1(_arrange({"x": 1920, "y": 1290}))
    hidden = first[0]["id"]
    d = _arrange({"x": 1920, "y": 1290}, minimize=[hidden])
    assert d["r"]["count"] == 4, ("the minimized window was left out", d["r"])
    w = next(x for x in d["out"] if x["id"] == hidden)
    assert not w["min"], ("still minimized after Grid", w)
    shown = [i for i, (m, data) in enumerate(d["log"]) if m == "wm-actions/set-minimized" and data.get("view_id") == hidden and data.get("state") is False]
    placed = [i for i, (m, data) in enumerate(d["log"]) if m == "window-rules/configure-view" and data.get("id") == hidden and data.get("geometry")]
    assert shown and placed and shown[0] < placed[0], d["log"]


def test_every_arranged_window_is_raised_above_the_desktop():
    """Placed is not visible: measured on the desk, a correct five-window grid that the person saw as
    'all the windows minimized' -- the full-monitor desktop surface was drawn over them."""
    d = _arrange({"x": 1920, "y": 1290})
    ids = {w["id"] for w in _dp1(d)}
    focused = [data.get("id") for m, data in d["log"] if m == "window-rules/focus-view"]
    assert ids <= set(focused), ("not every arranged window was raised", focused, ids)
    last_place = max(i for i, (m, data) in enumerate(d["log"]) if m == "window-rules/configure-view" and data.get("geometry"))
    first_focus = min(i for i, (m, _) in enumerate(d["log"]) if m == "window-rules/focus-view")
    assert first_focus > last_place, "raised before the windows were placed"


def test_a_window_pulled_back_after_it_lands_is_put_back():
    """Measured: Concord returned to its old slot a quarter-second after a correct placement -- three tiles
    and a hole, 'you have to tile twice for it to work'."""
    before = {w["id"]: w["g"] for w in _dp1(_arrange({"x": 1920, "y": 1290}, revert={}))}
    raw = json.loads(FIX.read_text())
    victim = next(v for v in raw["views"] if v.get("title", "").endswith("concord"))
    d = _arrange({"x": 1920, "y": 1290}, revert={str(victim["id"]): victim["geometry"]})
    got = next(w for w in d["out"] if w["id"] == victim["id"])
    assert got["g"] == before[victim["id"]], ("left where it was pulled back to", got["g"], before[victim["id"]])
    assert d["r"].get("replaced", 0) >= 1, d["r"]
