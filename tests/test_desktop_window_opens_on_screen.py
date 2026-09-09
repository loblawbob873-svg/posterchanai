"""A PosterChan window may never OPEN partly outside the monitor it opens on.

Run: venv-unified/bin/python -m pytest tests/test_desktop_window_opens_on_screen.py

THE BUG THIS EXISTS FOR — "social on desktop is some weird window that is half off the monitor".

On PosterChanOS an app launched from an icon or the start menu is a real compositor toplevel, not an
in-page `.osw` frame. Wayland gives a client NO SAY in where its toplevel goes — xdg-shell has no
positioning, which is why desktop/main.js has to ask the compositor to move the start menu after it
maps — so the only geometry this desktop controls is the SIZE, and Wayfire centres a window it is
given no position for. A centred window bigger than the output hangs off BOTH edges, with no title
bar left on screen to drag it back by.

The size travels os.js `_windowOpenHint` → oswin.js `open()` → `window.open(url, '_blank',
'width=…,height=…')` → Electron's `overrideBrowserWindowOptions.width/height`. Every one of those is
expressed in the PAGE'S OWN pixels: `window.open` features are CSS pixels and an Electron
BrowserWindow's width is DIP, which on Wayland is the same logical pixel. It is also what the OTHER
caller of that same parameter passes — `popOut` hands over `w.el.getBoundingClientRect()` with no
conversion at all.

`_windowOpenHint` multiplied that answer by `scaleFrom()` as well — the shell's own window measured
in the COMPOSITOR's units over its CSS viewport. That factor belongs to `mapRect`, which hands a
rectangle to `pcWM.place`; Electron applies it itself, which is why main.js's `placePopupWindow`
derives the same ratio as `sx`/`sy` to convert a popup's requested size BACK for the client. Measured
with the shipped arithmetic on a 3840x2160 desk whose compositor rectangle is twice the renderer's
viewport, the launcher asked for a **6436x3710** window — centred, that is 1298px off the left edge
and 1298px off the right. At an output scale of 1.25 it is 4023x2319: 91px off each side and 79 off
the top and bottom.

None of it is visible from here. At scale 1 every number is already correct, so every check on this
box passed while the reported machine opened Social a third of the way off the screen.

The numbers below are asserted, never the source text — a geometry bug is measured.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "static", "js", "client")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# Real outputs. The third column is the ratio between the compositor's rectangle for the shell and
# the CSS viewport that same shell reports — 1.0 is the machine this repo is developed on, which is
# why the bug was invisible here; 2.0 is an ordinary HiDPI panel and 1.25 the fractional scale the
# desk in os.js's own comments is running.
OUTPUTS = [
    ("1366x768 laptop", 1366, 768, 1.0),
    ("1920x1080 monitor", 1920, 1080, 1.0),
    ("1024x600 tablet", 1024, 600, 1.0),
    ("2560x1440 @1.25", 2560, 1440, 1.25),
    ("3840x2160 @2", 3840, 2160, 2.0),
    ("3072x2048 @1.25", 3072, 2048, 1.25),
]

# One of each shape place() knows about — a workbench, a column, a board, a game, and the default
# reading column. Social is 'global', and it is a workbench, i.e. the largest of them.
VIEWS = ["global", "terminal", "messages", "chess", "webxdc", "notes", "home"]

BOOT = """
const dir = %s;
global.window = { addEventListener(){}, innerWidth: 1920, innerHeight: 1080 };
global.document = { addEventListener(){}, querySelector(){ return null },
                    querySelectorAll(){ return [] } };
global.getComputedStyle = () => ({ zoom: String(ZOOM) });
const NAT = window.PCOSNative = require(dir + '/osnative.js');
require(dir + '/os.js');
const PCOS = window.PCOS;
""" % json.dumps(CLIENT)


def _node(script, zoom=1.0):
    src = "const ZOOM = %r;\n" % float(zoom) + BOOT + script
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout or "null")


def _measure(zoom=1.0):
    """Run the SHIPPED `windowOpenHint` and `place` for every output and every window shape.

    `old` re-runs the arithmetic as it was before the fix — place() x zoom x the compositor scale —
    so the same harness reports what the machine was actually asking for."""
    return _node("""
      const outs = %s, views = %s, got = [];
      for(const [name, w, h, s] of outs){
        window.innerWidth = w; window.innerHeight = h;
        const shell = { x: 0, y: 0, width: Math.round(w * s), height: Math.round(h * s) };
        const scale = NAT.scaleFrom(shell, w, h);
        for(const v of views){
          const p = PCOS.__place(0, v);
          got.push({ name, view: v, cssW: w, cssH: h, scale: s, place: p,
                     hint: PCOS.windowOpenHint(v),
                     old: { width: Math.round(p.w * ZOOM * scale.x),
                            height: Math.round(p.h * ZOOM * scale.y) } });
        }
      }
      console.log(JSON.stringify(got));
    """ % (json.dumps(OUTPUTS), json.dumps(VIEWS)), zoom=zoom)


@pytest.mark.parametrize("zoom", [1.0, 0.72])
def test_a_window_is_never_opened_larger_than_the_monitor_it_opens_on(zoom):
    """The regression. `window.open`'s width/height are the OPENER'S pixels, so the answer has to
    fit the opener's own viewport — on every output, at every display scale, for every shape."""
    bad = []
    for row in _measure(zoom):
        hint = row["hint"]
        assert hint, row
        if hint["width"] > row["cssW"] or hint["height"] > row["cssH"]:
            bad.append("%s: %s asked for %dx%d on a %dx%d screen"
                       % (row["name"], row["view"], hint["width"], hint["height"],
                          row["cssW"], row["cssH"]))
    assert not bad, (
        "a window is created larger than its output, and Wayland lets no client say where its "
        "toplevel goes — the compositor centres it and the overhang is off BOTH edges:\n  "
        + "\n  ".join(bad))


def test_this_check_would_have_caught_the_pre_fix_rule():
    """A test that cannot fail is not a test. Re-run the arithmetic that shipped and require it to
    produce exactly the screen this was reported from, so the assertion above has teeth."""
    rows = {(r["name"], r["view"]): r for r in _measure()}
    hidpi = rows[("3840x2160 @2", "global")]
    assert hidpi["old"] == {"width": 6436, "height": 3710}, hidpi
    assert hidpi["hint"] == {"width": 3218, "height": 1855}, hidpi

    frac = rows[("3072x2048 @1.25", "global")]
    assert frac["old"]["width"] > frac["cssW"] and frac["old"]["height"] > frac["cssH"], frac
    assert frac["hint"]["width"] <= frac["cssW"] and frac["hint"]["height"] <= frac["cssH"], frac

    # And at scale 1 the old rule was already right, which is the whole reason this went unseen.
    plain = rows[("1920x1080 monitor", "global")]
    assert plain["old"] == {"width": plain["hint"]["width"], "height": plain["hint"]["height"]}


def test_the_hint_is_the_size_place_chose_converted_only_by_the_page_zoom():
    """Pin the unit rather than the value: `popOut` passes a `getBoundingClientRect()` straight
    through, so the launcher's answer is in the same space or one of the two callers is wrong."""
    for zoom in (1.0, 0.72):
        for row in _measure(zoom):
            want_w = min(round(row["place"]["w"] * zoom), row["cssW"])
            want_h = min(round(row["place"]["h"] * zoom), row["cssH"])
            assert row["hint"] == {"width": want_w, "height": want_h}, (zoom, row)


def test_the_third_argument_of_windowOpenSize_is_a_ceiling_not_a_multiplier():
    """The pure function, driven directly. It used to take `scaleFrom()`'s answer and MULTIPLY by
    it; it now takes the usable area and clamps to it. Both are `{x, y}`-vs-`{width, height}`
    objects in the same argument slot, so a scale accidentally left at a call site must be ignored
    rather than silently doubling every window."""
    got = _node("""
      console.log(JSON.stringify({
        fits: NAT.windowOpenSize({w:1200,h:800}, 1, {width:1920,height:1080}),
        clamped: NAT.windowOpenSize({w:4000,h:3000}, 1, {width:1920,height:1080}),
        scaleShaped: NAT.windowOpenSize({w:1200,h:800}, 1, {x:2,y:2,ox:0,oy:0}),
        zoomed: NAT.windowOpenSize({w:1260,h:950}, 2, {width:3840,height:2160}),
        nothing: NAT.windowOpenSize({w:0,h:0}, 1, {width:1920,height:1080}),
        silly: NAT.windowOpenSize({w:4,h:3}, 1, {width:1920,height:1080})}));
    """)
    assert got["fits"] == {"width": 1200, "height": 800}
    assert got["clamped"] == {"width": 1920, "height": 1080}
    assert got["scaleShaped"] == {"width": 1200, "height": 800}
    assert got["zoomed"] == {"width": 2520, "height": 1900}
    # A size that could not be worked out must fall through to oswin.js's own default, never to a
    # window a few pixels across.
    assert got["nothing"] is None and got["silly"] is None


def test_the_bound_is_the_viewport_the_page_is_actually_drawn_in():
    """`_windowOpenHint` reads `visualViewport` where there is one and `innerWidth` otherwise, and
    the bound must be that same measurement — the shell's compositor rectangle describes the same
    area in units this call does not speak."""
    got = _node("""
      window.innerWidth = 1920; window.innerHeight = 1080;
      window.visualViewport = { width: 800, height: 500 };
      console.log(JSON.stringify({ hint: PCOS.windowOpenHint('global'),
                                   place: PCOS.__place(0, 'global') }));
    """)
    assert got["place"]["w"] > 800, got     # place() measured the 1920px desk…
    assert got["hint"] == {"width": 800, "height": 500}, got   # …the window fits the real viewport


def test_place_keeps_every_window_inside_the_desk_it_measured():
    """The in-page half of the same rule: a `.osw` frame's own rectangle, cascade included."""
    bad = []
    for row in _measure():
        r, w, h = row["place"], row["cssW"], row["cssH"]
        # place() answers in LAYOUT pixels of the desk, which at zoom 1 with no #os-desk element is
        # the viewport minus the taskbar (os.js TASKBAR = 48).
        if r["x"] < 0 or r["y"] < 0 or r["x"] + r["w"] > w or r["y"] + r["h"] > h - 48:
            bad.append("%s: %s at %s on a %dx%d desk" % (row["name"], row["view"], r, w, h - 48))
    assert not bad, "a window opens outside the desktop:\n  " + "\n  ".join(bad)


OSWIN = """
const vm = require('node:vm'), fs = require('node:fs');
const asked = [];
const ctx = {
  pcWM: {}, PCOSShell: { available: () => true },
  localStorage: { getItem: () => null },
  location: { pathname: '/client', search: '' },
  document: { querySelector: (sel) => (/nav-item/.test(sel) ? {} : null) },
  screen: { availWidth: SCREEN_W, availHeight: SCREEN_H },
  open: (url, target, features) => { asked.push(features); return { focus(){} }; },
  BroadcastChannel: function(){ this.postMessage = () => {}; this.close = () => {}; },
};
ctx.globalThis = ctx; ctx.window = ctx; ctx.root = ctx;
vm.runInNewContext(fs.readFileSync(%s, 'utf8'), ctx);
ctx.PCOSWin.open('global', 'Social', OPTS);
console.log(JSON.stringify(asked));
""" % json.dumps(os.path.join(CLIENT, "oswin.js"))


def _oswin(screen_w, screen_h, opts):
    src = (OSWIN.replace("SCREEN_W", str(screen_w)).replace("SCREEN_H", str(screen_h))
           .replace("OPTS", json.dumps(opts)))
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-3000:]
    got = json.loads(out.stdout or "[]")
    assert got, "PCOSWin.open never called window.open — the stubs no longer satisfy enabled()"
    feat = dict(p.split("=", 1) for p in got[0].split(","))
    return int(feat["width"]), int(feat["height"])


def test_the_fallback_size_is_not_bigger_than_a_small_monitor():
    """`open()` falls back to 1100x760 whenever no hint reaches it — a size that is ALREADY off the
    screen on a 1024x600 machine, which is the one case where a misplaced window cannot be dragged
    back into view."""
    w, h = _oswin(1024, 600, {})
    assert w <= 1024 and h <= 600, (
        "with no size hint a window is opened at %dx%d on a 1024x600 screen" % (w, h))


def test_a_supplied_size_is_clamped_to_the_screen_too():
    """popOut hands over the frame's own rectangle, and a frame that filled a big monitor is bigger
    than the small one it may be opened on. The clamp is in the one place both callers go through,
    not in each of them."""
    w, h = _oswin(1366, 768, {"width": 3000, "height": 2000})
    assert w <= 1366 and h <= 768, "an oversized request opened a %dx%d window on 1366x768" % (w, h)


def test_a_reasonable_request_is_passed_through_unchanged():
    """The clamp is a ceiling, not a resize: a window that fits is opened exactly as asked."""
    assert _oswin(1920, 1080, {"width": 1280, "height": 800}) == (1280, 800)


def test_an_unmeasurable_screen_imposes_no_ceiling():
    """A guessed bound is worse than none — a browser or shell that reports no work area must not
    silently shrink every window to a number nothing measured."""
    assert _oswin(0, 0, {"width": 1600, "height": 1000}) == (1600, 1000)
