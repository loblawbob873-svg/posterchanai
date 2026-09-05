"""THE TASKBAR IS THE ONE PART OF THIS DESKTOP THAT NOTHING OUTSIDE IT KNOWS ABOUT.

It is painted at the bottom of the shell's own surface, and the shell is the TILED window that
every native application floats above — so a window over that band does not overlap the bar, it
hides it, and no stacking order anywhere puts it back. Reserving space is normally a layer-shell
exclusive zone; an Electron toplevel cannot make one, so Wayfire answers its WHOLE output as the
work area and its `place` plugin, its maximise and every application's own remembered geometry are
free to land on the bar.

MEASURED on the real desk — two 3072x2048 outputs at scale 1.25, the shell renderer alive from
before the launch until after the reading — `firefox-bin` started from the start menu opened on DP-2
at geometry {3205, 47, 2913, 2080}: 2080 tall on a 2048-tall screen, i.e. past the bottom edge, with
its decoration reaching 29 units further still. It stayed there for the twenty-five seconds it was
watched, because nothing in this desktop places a window it does not host and hosting is off by
default. The taskbar is the bottom 38 of those units.

Everything below runs the SHIPPED code with those numbers.


VIEW GEOMETRY IS OUTPUT-LOCAL, AND THIS WAS RE-MEASURED RAW BEFORE TRUSTING EITHER ACCOUNT.
`wf.views()` in the on-box helper TRANSLATES to global coordinates, so a reading taken through it
looks global and is not. Asked with `globalise=False`, on a two-monitor desk with DP-2 at x=3072:

    'PosterChan · Nostr'  out=DP-1  geo x=0        'PosterChan · Nostr'  out=DP-2  geo x=0
    'PosterChan Window — terminal'  out=DP-2  geo x=39
    'PosterChan Window — global'    out=DP-2  geo x=986

Both shell surfaces answer x=0 despite being on different outputs, and every window on DP-2 answers
an x far below 3072. `base-geometry` is output-local too. So `wm-wayfire.js`'s `_toGlobal` is
correct and the vertical rule below is not the only half that can be trusted.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE = os.path.join(ROOT, "static", "js", "client", "osnative.js")
NODE = shutil.which("node") or shutil.which("nodejs")
needs_node = pytest.mark.skipif(not NODE, reason="needs node")

# The desk as it really was, in the two coordinate systems it is measured in at once.
SHELL_DP2 = {"x": 3072, "y": 0, "width": 3072, "height": 2048}   # compositor units
CSS_W, CSS_H = 3840, 2560                                        # the renderer's own pixels
DESK_BOTTOM = CSS_H - 48                                         # #os-desk stops above the bar
FIREFOX = {"id": 11, "app": "firefox", "below": 29, "above": 23,
           "rect": {"x": 3205, "y": 47, "width": 2913, "height": 2080}}


def run_node(body, module=NATIVE):
    src = ("const N = require(%s);\nconst out = {};\n%s\nprocess.stdout.write(JSON.stringify(out));"
           % (json.dumps(module), body))
    return raw_node(src)


def raw_node(src):
    r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


SETUP = """
  const shell = %s, rows = [%s];
  const scale = N.scaleFrom(shell, %d, %d);
  const area = N.workAreaFrom(shell, {bottom: %d}, %d, scale);
""" % (json.dumps(SHELL_DP2), json.dumps(FIREFOX), CSS_W, CSS_H, DESK_BOTTOM, CSS_H)


# ── the measurement ────────────────────────────────────────────────────────────────────────────
@needs_node
def test_the_reserved_band_is_measured_from_the_desk_not_from_the_48px_constant():
    """A CSS pixel here is not a compositor unit, and the constant is wrong at some zooms anyway."""
    out = run_node(SETUP + "out.area = area;")
    assert out["area"] == {"x": 3072, "y": 0, "w": 3072, "h": 2010, "reserve": 38}, out["area"]


@needs_node
def test_an_unmeasurable_desk_reserves_nothing_rather_than_guessing():
    """"I could not measure it" is never "there is no taskbar" — and never a guessed band either.
    A desk read while the shell was hidden is 0 high, which as a reserve pins every window on the
    machine into a strip."""
    out = run_node("""
      const shell = {x:0, y:0, width:3072, height:2048};
      const scale = N.scaleFrom(shell, 3840, 2560);
      out.none  = N.workAreaFrom(shell, null, 2560, scale);
      out.zero  = N.workAreaFrom(shell, {bottom: 0}, 2560, scale);
      out.silly = N.workAreaFrom(shell, {bottom: 100}, 2560, scale);   // would reserve 1968 of 2048
      out.noshell = N.workAreaFrom(null, {bottom: 2512}, 2560, scale);
    """)
    for key in ("none", "zero", "silly"):
        assert out[key]["reserve"] == 0, key
        assert out[key]["h"] == 2048, key          # the whole output: today's behaviour, unchanged
    assert out["noshell"] is None


# ── the decision ───────────────────────────────────────────────────────────────────────────────
@needs_node
def test_the_firefox_that_was_reported_is_lifted_off_the_bar():
    """THE BUG. Without a work area there is no plan at all and the window keeps the rectangle it
    was measured with; with one it is shortened until its DECORATED bottom meets the top of the
    bar — 2010, the exact edge, not an approximation of it. The overhang comes off the BOTTOM, so
    the window is still where its owner left it and its title bar is still on the screen."""
    out = run_node(SETUP + """
      let p = N.taskbarPlan(rows, area, null);
      out.first = p.place;
      p = N.taskbarPlan(rows, area, p.seen);
      out.settled = p.place;
      out.noarea = N.taskbarPlan(rows, null, new Map([[11, '3205,47,2913,2080']])).place;
    """)
    assert out["first"] == [], "a rectangle seen once is a window still moving, not an answer"
    assert out["settled"] == [{"id": 11, "rect": {"x": 3205, "y": 47, "w": 2913, "h": 1934}}]
    fixed = out["settled"][0]["rect"]
    assert fixed["y"] + fixed["h"] + FIREFOX["below"] == 2010, "the bar's top edge, exactly"
    assert fixed["y"] == FIREFOX["rect"]["y"], "shrunk, not moved"
    assert fixed["y"] - FIREFOX["above"] >= 0, "the title bar stays on the screen"
    assert out["noarea"] == [], "with nothing measured this must do nothing at all"


@needs_node
def test_the_correction_is_vertical_only():
    """The taskbar is a band across the bottom, so only y and height decide whether a window is on
    it. Sliding somebody's window sideways to keep a bar visible is a correction nobody asked for."""
    out = run_node(SETUP + """
      const key = new Map([[11, '3205,47,2913,2080']]);
      out.p = N.taskbarPlan(rows, area, key).place;
    """)
    assert out["p"][0]["rect"]["x"] == FIREFOX["rect"]["x"]
    assert out["p"][0]["rect"]["w"] == FIREFOX["rect"]["width"]


@needs_node
def test_the_decoration_below_the_content_is_part_of_what_covers_the_bar():
    """What a person sees on the bar is the DECORATED frame. Judged from `geometry` alone the
    correction is 29 units short and a strip of bar stays covered, which reads as no fix at all."""
    out = run_node(SETUP + """
      const bare = [Object.assign({}, rows[0], {below: 0})];
      const key = new Map([[11, '3205,47,2913,2080']]);
      out.withDecor = N.taskbarPlan(rows, area, key).place[0].rect.h;
      out.without   = N.taskbarPlan(bare, area, key).place[0].rect.h;
    """)
    assert out["without"] - out["withDecor"] == FIREFOX["below"]


@needs_node
def test_a_window_dragged_bodily_onto_the_bar_is_moved_rather_than_squeezed():
    """Shrinking is the gentler answer and it is tried first, but a 400px window sitting at y=1900
    can only be made to fit by becoming 81px tall, which is not a window any more."""
    out = run_node(SETUP + """
      const low = [{id:12, below:29, above:23, rect:{x:3200, y:1900, width:600, height:400}}];
      const key = new Map([[12, '3200,1900,600,400']]);
      out.p = N.taskbarPlan(low, area, key).place;
    """)
    fixed = out["p"][0]["rect"]
    assert fixed["h"] == 400, "its size is not the problem; where it is put is"
    assert fixed["y"] + fixed["h"] + 29 == 2010
    assert fixed["y"] - 23 >= 0


@needs_node
def test_a_window_at_rest_above_the_bar_is_never_touched():
    """Correcting a window that is already fine is a compositor round trip and a relayout per pass,
    for ever — which is how a browser is made to reflow while nobody is doing anything."""
    out = run_node(SETUP + """
      const ok = [{id:5, rect:{x:3100, y:40, width:1200, height:900}, below:29, above:23}];
      const key = new Map([[5, '3100,40,1200,900']]);
      out.p = N.taskbarPlan(ok, area, key).place;
    """)
    assert out["p"] == []


@needs_node
def test_the_shell_a_fullscreen_game_and_a_minimised_window_are_all_left_alone():
    """The shell surface FILLS the output, band included — "correcting" it shrinks the desktop out
    from under its own taskbar, every pass. A fullscreen application owns the screen by definition,
    and a minimised one is nowhere."""
    out = run_node(SETUP + """
      const many = [
        {id:1, own:true,        rect:{x:3072,y:0,width:3072,height:2048}},
        {id:2, fullscreen:true, rect:{x:3072,y:0,width:3072,height:2048}},
        {id:3, stashed:true,    rect:{x:3072,y:0,width:3072,height:2048}},
        {id:4,                  rect:{x:0,   y:0,width:3000,height:2040}}];  // the OTHER output
      const key = new Map(many.map(m => [m.id, m.rect.x+','+m.rect.y+','
                                              +m.rect.width+','+m.rect.height]));
      out.p = N.taskbarPlan(many, area, key).place;
    """)
    assert out["p"] == [], "each of these has its own reason to be untouchable"


# ── the compositor half ────────────────────────────────────────────────────────────────────────
@needs_node
def test_the_wayfire_backend_reads_the_decoration_that_reaches_past_the_content():
    out = run_node("""
      const pick = v => { const r = N.normalizeView(v); return {above: r.above, below: r.below}; };
      out.real = pick({id: 11, 'app-id': 'firefox',
        geometry: {x:3205, y:47, width:2913, height:2080},
        'base-geometry': {x:107, y:24, width:2965, height:2132}});
      out.none = pick({id: 12, geometry: {x:0,y:0,width:10,height:10}});
      out.mad  = pick({id: 13, geometry: {x:0, y:2000, width:10, height:10},
        'base-geometry': {x:0, y:0, width:20, height:60}});
    """, module=os.path.join(ROOT, "desktop", "wm-wayfire.js"))
    assert out["real"] == {"above": 23, "below": 29}, "measured on Wayfire 0.10.1, border_size 3"
    assert out["none"] == {"above": 0, "below": 0}, "no base geometry is no claim, not a negative one"
    assert out["mad"] == {"above": 0, "below": 50}, \
        "an implausible offset falls back to the total, which over-reserves — the safe direction"


@needs_node
def test_a_maximise_stops_at_the_measured_bar_instead_of_the_guessed_72():
    """`b.height - 72` was the taskbar, guessed: 72 is the 48px bar at ONE zoom. On this desk the
    bar is 38 units, so a maximised window stopped 34 short of it — and scaled the other way the
    same constant leaves the bar covered."""
    out = raw_node("""
      const out = {};
      const {WayfireWM} = require(%s);
      const wm = new WayfireWM('/nonexistent.sock');
      const sent = [];
      wm._send = (method, data) => {
        if(method === 'window-rules/list-outputs')
          return Promise.resolve([{id:3, name:'DP-2',
            geometry:{x:3072,y:0,width:3072,height:2048}}]);
        if(method === 'window-rules/list-views')
          return Promise.resolve([{id:11, 'app-id':'firefox', 'output-id':3,
            geometry:{x:3200,y:100,width:1000,height:800}}]);
        sent.push([method, data]);
        return Promise.resolve({});
      };
      (async () => {
        await wm.snap(11, 'max');
        out.guessed = sent.pop()[1].geometry;
        await wm.setWorkArea({x:3072, y:0, w:3072, h:2010, reserve:38});
        wm._forgetOutputs();
        await wm.snap(11, 'max');
        out.measured = sent.pop()[1].geometry;
        process.stdout.write(JSON.stringify(out));
      })();
    """ % json.dumps(os.path.join(ROOT, "desktop", "wm-wayfire.js")))
    # configure-view geometry is output-LOCAL, hence x:0 rather than 3072.
    assert out["guessed"] == {"x": 0, "y": 0, "width": 3072, "height": 2048 - 72}
    assert out["measured"] == {"x": 0, "y": 0, "width": 3072, "height": 2010}


@needs_node
def test_a_placement_is_clamped_into_the_work_area_not_the_whole_output():
    """`place` is the one door every compositor-side rectangle goes through."""
    out = run_node("""
      const {clampRectToOutputs} = require(%s);
      const bare = [{name:'DP-2', rect:{x:3072,y:0,width:3072,height:2048}}];
      const with_ = [Object.assign({}, bare[0], {work:{x:3072,y:0,w:3072,h:2010,reserve:38}})];
      const want = {x:3400, y:1900, w:1000, h:800};
      out.bare = clampRectToOutputs(want, bare);
      out.with = clampRectToOutputs(want, with_);
    """ % json.dumps(os.path.join(ROOT, "desktop", "wm.js")))
    assert out["bare"]["y"] + out["bare"]["h"] == 2048, "today: the bar is fair game"
    assert out["with"]["y"] + out["with"]["h"] == 2010, "the band is not somewhere a window may go"


@needs_node
def test_only_the_output_that_published_an_area_is_bound_by_it():
    """Two shell renderers, one per screen. One monitor's measurement must not govern the other's,
    and an output nobody has measured keeps behaving exactly as it did before this existed."""
    out = run_node("""
      const {rememberWorkArea, workAreaFor} = require(%s);
      let store = [];
      store = rememberWorkArea(store, {x:0, y:0, w:3072, h:2010, reserve:38});
      store = rememberWorkArea(store, {x:3072, y:0, w:3072, h:2010, reserve:38});
      store = rememberWorkArea(store, {x:0, y:0, w:3072, h:2000, reserve:48});  // republished
      out.count = store.length;
      out.dp1 = workAreaFor(store, {x:0, y:0, width:3072, height:2048});
      out.dp2 = workAreaFor(store, {x:3072, y:0, width:3072, height:2048});
      out.dp3 = workAreaFor(store, {x:6144, y:0, width:1920, height:1080});
      out.junk = rememberWorkArea(store, {x:9, y:9, w:0, h:0}).length;
    """ % json.dumps(os.path.join(ROOT, "desktop", "wm.js")))
    assert out["count"] == 2, "a display change republishes; it does not accumulate"
    assert out["dp1"]["h"] == 2000 and out["dp2"]["h"] == 2010
    assert out["dp3"] is None, "an unmeasured output is not handed somebody else's band"
    assert out["junk"] == 2, "an empty rectangle is not a measurement"


# ── the wiring, which is where a rule like this dies quietly ───────────────────────────────────
def test_the_guard_runs_on_the_pass_that_sees_windows_nobody_here_hosts():
    """`nsync` returns immediately unless a native window is HOSTED — which is exactly the case this
    bug does not happen in. `adoptAll` is the pass that runs whether anything is hosted or not."""
    src = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
    guard = src[src.index("async function _guardTaskbar("):src.index("/* Whatever the compositor has")]
    # To the END of the function, not a fixed 2500 characters: a slice measured in bytes stops
    # reaching the call it is looking for the moment anything above it grows, and then reports
    # "defined and never called" about code that is still there.
    _a = src.index("async function adoptAll(){")
    adopt = src[_a:src.index("\n  async function ", _a + 10)]
    assert "_guardTaskbar(list, shellId)" in adopt, "the guard is defined and never called"
    assert "NAT().taskbarPlan(" in guard, "the decision is never consulted"
    assert "pcWM.place(" in guard, "a plan nothing performs"
    assert "pcWM.workArea" in guard, "the compositor half is never told the band"
    assert "hosted.has(Number(r.id))" in guard, "a hosted window already has an owner"
    assert "place\\.poster\\.desktop" in guard, "our own surfaces must be excluded"
    # Every popout shares the shell's app id, so "the first PosterChan window" can pick a 1509x1094
    # frame as the output and measure the whole band against it.
    assert "Number.isFinite(shellId) ? list.find" in guard, "the shell surface must not be guessed"
    nsync = src[src.index("async function nsync(){"):]
    assert "_guardTaskbar" not in nsync[:9000], "nsync is gated on hosting; the guard must not be"


def test_the_measurement_reaches_the_compositor_process():
    preload = open(os.path.join(ROOT, "desktop", "preload.js"), encoding="utf-8").read()
    main = open(os.path.join(ROOT, "desktop", "main.js"), encoding="utf-8").read()
    assert re.search(r"workArea:\s*\(area\)\s*=>\s*ipcRenderer\.invoke\('pc:wm:workarea'", preload)
    assert "ipcMain.handle('pc:wm:workarea'" in main
    assert "setWorkArea" in main, "the handler must reach the backend"
