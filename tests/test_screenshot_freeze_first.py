"""A region screenshot on PosterChanOS is chosen on a FROZEN screen and cut out of the frame taken first.

Reported: "Screen Shot on PosterChanOS -> take the screenshot then let the user choose the area or
whatever. It's hard to take a selection when the screen is moving." The region used to be picked with
`slurp` over the LIVE screen and then photographed with `grim -g` -- with a video playing, the thing
being framed moved under the mouse, and the picture was of the moment the button was released.

Now: grim captures the whole layout FIRST, the still picture is laid over every output
(desktop/shot-freeze.js), slurp draws over it, and the rectangle is cut from the frame already taken
(no second capture). slurp answers in LOGICAL layout coordinates and grim's picture is the layout's
bounding box at one scale, so the crop has to account for HiDPI scale and output offsets.

Everything here RUNS shipped code: desktop/screenshot.js and desktop/shot-freeze.js under node (stub
grim/slurp, fake compositor), and the compositor-key script pc-screenshot under sh with stub grim /
slurp / wlr-randr and a real Pillow crop.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHOT = ROOT / "desktop/screenshot.js"
FREEZE = ROOT / "desktop/shot-freeze.js"
SCRIPT = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-screenshot"
NODE = shutil.which("node") or shutil.which("nodejs")

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

# A scale-2 laptop panel at the origin and a scale-1 monitor to its right, 50px lower.
# Logical bounding box: x 0..500, y 0..200 -> grim's picture at the highest scale (2): 1000x400.
LAYOUT = [{"name": "eDP-1", "x": 0, "y": 0, "width": 300, "height": 200},
          {"name": "DP-1", "x": 300, "y": 50, "width": 200, "height": 150}]
RANDR = [
    {"name": "eDP-1", "enabled": True, "scale": 2.0, "transform": "normal", "position": {"x": 0, "y": 0},
     "modes": [{"width": 600, "height": 400, "refresh": 60.0, "current": True}]},
    {"name": "DP-1", "enabled": True, "scale": 1.0, "transform": "normal", "position": {"x": 300, "y": 50},
     "modes": [{"width": 1920, "height": 1080, "current": False}, {"width": 200, "height": 150, "current": True}]},
    {"name": "HDMI-A-1", "enabled": False, "scale": 1.0, "position": {"x": 0, "y": 0},
     "modes": [{"width": 1920, "height": 1080, "current": True}]},
]


def _stub(d: Path, name: str, body: str, shebang="#!/bin/sh"):
    p = d / name
    p.write_text(shebang + "\n" + body)
    p.chmod(0o755)
    return p


# ── desktop/screenshot.js ────────────────────────────────────────────────────────────────────────────

GRIM_PY = r'''
import os, struct, sys, zlib
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("grim " + " ".join(args) + "\n")
w, h = int(os.environ.get("STUB_W", "1000")), int(os.environ.get("STUB_H", "400"))
if "-g" in args:
    w, h = 7, 7
ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
open(args[-1], "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b""))
'''


def _run_js(body, env):
    js = """
    const S = require(%s);
    const fs = require('fs'), path = require('path');
    const LOG = process.env.STUB_LOG;
    const log = (s) => fs.appendFileSync(LOG, s + '\\n');
    (async () => { const out = {};
      try { %s } catch (e) { out.threw = String(e && e.stack || e); }
      process.stdout.write(JSON.stringify(out)); })();
    """ % (json.dumps(str(SHOT)), body)
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60, env={**os.environ, **env})
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def _js_world(tmp_path, slurp_out="350,60 100x80", slurp_rc=0):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    home, tmpd = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    tmpd.mkdir()
    log = tmp_path / "calls.log"
    grim = _stub(bin_, "grim", GRIM_PY, shebang="#!/usr/bin/env python3")
    slurp = _stub(bin_, "slurp", f'echo slurp >>"{log}"\n' + (f'echo "{slurp_out}"\n' if slurp_rc == 0 else
                                                                   'echo "selection cancelled" >&2\nexit 1\n'))
    env = {"PC_GRIM": str(grim), "PC_SLURP": str(slurp), "HOME": str(home), "TMPDIR": str(tmpd),
           "STUB_LOG": str(log), "PATH": f"{bin_}:{os.environ['PATH']}"}
    return env, log, home, tmpd


HOOKS = """
  const layout = %s;
  const hooks = {
    layout,
    showFrozen: async (src, lay) => {
      log('show ' + path.basename(src) + ' exists=' + fs.existsSync(src) + ' outputs=' + lay.length);
      return async () => log('hide');
    },
    crop: async (src, rect, dest) => {
      log('crop ' + path.basename(src) + ' ' + JSON.stringify(rect));
      fs.writeFileSync(dest, 'CROPPED');
    },
  };
""" % json.dumps(LAYOUT)


@pytest.mark.skipif(not NODE, reason="node required")
def test_the_whole_screen_is_captured_before_the_region_is_chosen_and_the_crop_is_cut_from_it(tmp_path):
    env, log, home, tmpd = _js_world(tmp_path)
    out = _run_js(HOOKS + """
      out.r = await S.capture(Object.assign({ mode: 'region' }, hooks));
      out.saved = out.r.ok ? fs.readFileSync(out.r.path, 'utf8') : null;
      out.left = fs.existsSync(S.stageDir()) ? fs.readdirSync(S.stageDir()) : [];
    """, env)
    calls = log.read_text().splitlines()
    assert out["r"]["ok"] is True, out
    assert out["r"]["frozen"] is True
    assert [c.split()[0] for c in calls] == ["grim", "show", "slurp", "hide", "crop"], calls
    assert calls[0].count(" ") == 1 and "-g" not in calls[0], "the frame must be the WHOLE layout, not a region"
    assert "exists=true outputs=2" in calls[1], "the overlay was not given the captured frame"
    assert calls[0].split()[1].endswith(calls[4].split()[1]), "the crop did not use the frame grim captured"
    # slurp "350,60 100x80" (logical) on the scale-2 picture: (350*2, 60*2, 100*2, 80*2)
    assert json.loads(calls[4].split(" ", 2)[2]) == {"x": 700, "y": 120, "width": 200, "height": 160}
    assert out["saved"] == "CROPPED" and "Pictures/Screenshots" in out["r"]["path"]
    assert out["left"] == [], "the frozen frame was left behind"


@pytest.mark.skipif(not NODE, reason="node required")
def test_escape_takes_the_overlay_down_and_leaves_nothing_behind(tmp_path):
    env, log, home, tmpd = _js_world(tmp_path, slurp_rc=1)
    out = _run_js(HOOKS + """
      out.r = await S.capture(Object.assign({ mode: 'region' }, hooks));
      out.left = fs.existsSync(S.stageDir()) ? fs.readdirSync(S.stageDir()) : [];
      out.shots = fs.existsSync(S.shotDir()) ? fs.readdirSync(S.shotDir()) : [];
    """, env)
    calls = [c.split()[0] for c in log.read_text().splitlines()]
    assert out["r"] == {"ok": False, "cancelled": True, "why": ""}
    assert calls == ["grim", "show", "slurp", "hide"], calls
    assert out["left"] == [] and out["shots"] == []


@pytest.mark.skipif(not NODE, reason="node required")
def test_the_print_screen_prompts_frame_is_reused_with_no_second_capture(tmp_path):
    """Print Screen stages the whole screen BEFORE its prompt is drawn; "Select region" chooses on that
    frame -- the moment of the keypress -- and consumes it."""
    env, log, home, tmpd = _js_world(tmp_path)
    out = _run_js(HOOKS + """
      const st = await S.capture({ mode: 'screen', stage: true });
      fs.writeFileSync(LOG, '');
      out.r = await S.capture(Object.assign({ mode: 'region', frozen: st.path }, hooks));
      out.stagedGone = !fs.existsSync(st.path);
    """, env)
    calls = log.read_text().splitlines()
    assert out["r"]["ok"] is True, out
    assert [c.split()[0] for c in calls] == ["show", "slurp", "hide", "crop"], calls
    assert out["stagedGone"] is True


@pytest.mark.skipif(not NODE, reason="node required")
def test_with_no_layout_it_is_the_live_picker_as_before(tmp_path):
    env, log, home, tmpd = _js_world(tmp_path)
    out = _run_js("out.r = await S.capture({ mode: 'region' });", env)
    calls = log.read_text().splitlines()
    assert out["r"]["ok"] is True
    assert calls == ["slurp", "grim -g 350,60 100x80 " + out["r"]["path"]], calls


@pytest.mark.skipif(not NODE, reason="node required")
def test_full_screen_mode_is_unchanged(tmp_path):
    env, log, home, tmpd = _js_world(tmp_path)
    out = _run_js(HOOKS + "out.r = await S.capture(Object.assign({ mode: 'screen' }, hooks));", env)
    calls = log.read_text().splitlines()
    assert out["r"]["ok"] is True
    assert calls == ["grim " + out["r"]["path"]], calls


@pytest.mark.skipif(not NODE, reason="node required")
def test_the_crop_arithmetic_handles_scale_offsets_and_a_monitor_left_of_the_origin(tmp_path):
    env, log, home, tmpd = _js_world(tmp_path)
    out = _run_js("""
      const L = %s;
      out.second = S.frozenCrop('350,60 100x80', L, { width: 1000, height: 400 });
      out.first = S.frozenCrop('10,20 30x40', L, { width: 1000, height: 400 });
      out.edge = S.frozenCrop('480,190 100x100', L, { width: 1000, height: 400 });
      // A 1x monitor LEFT of a 2x panel: the box starts at x=-400.
      const N = [{ x: -400, y: 0, width: 400, height: 300 }, { x: 0, y: 0, width: 600, height: 300 }];
      out.left = S.frozenCrop('-390,10 100x50', N, { width: 2000, height: 600 });
      out.mismatch = S.frozenCrop('10,10 10x10', L, { width: 1000, height: 1000 });
      out.cancel = S.frozenCrop('', L, { width: 1000, height: 400 });
    """ % json.dumps(LAYOUT), env)
    assert out["second"] == {"x": 700, "y": 120, "width": 200, "height": 160}
    assert out["first"] == {"x": 20, "y": 40, "width": 60, "height": 80}
    assert out["edge"] == {"x": 960, "y": 380, "width": 40, "height": 20}, "a selection past the edge is clamped"
    assert out["left"] == {"x": 20, "y": 20, "width": 200, "height": 100}
    assert out["mismatch"] is None, "a layout that is not the one grim photographed must not be guessed at"
    assert out["cancel"] is None


# ── desktop/shot-freeze.js ───────────────────────────────────────────────────────────────────────────

FREEZE_JS = r"""
const F = require(%(mod)s);
const fs = require('fs');
const calls = [], wins = [];
let nextId = 50;
const views = [];
const wm = () => ({
  windows: async () => views.slice(),
  place: async (id, x, y, w, h) => calls.push(['place', id, x, y, w, h]),
  alwaysOnTop: async (id, on) => calls.push(['top', id, on]),
});
function createWindow(o){
  const w = { o, destroyed: false, shown: false, page: '',
    on(){}, isDestroyed(){ return this.destroyed; }, destroy(){ this.destroyed = true; calls.push(['destroy', o.title]); },
    async loadFile(p){ this.page = p; },
    showInactive(){ this.shown = true; views.push({ id: nextId++, title: o.title }); },
    webContents: { executeJavaScript: async (s) => calls.push(['reveal', o.title, s]) } };
  wins.push(w); return w;
}
(async () => {
  const host = F.createFreezeHost({ wm, createWindow, sleep: async () => {}, settleMs: 0 });
  const hide = await host.show(%(src)s, %(layout)s);
  const out = { calls: calls.slice(), wins: wins.map(w => ({ title: w.o.title, w: w.o.width, h: w.o.height,
    focusable: w.o.focusable, top: w.o.alwaysOnTop, shown: w.shown, html: fs.readFileSync(w.page, 'utf8'), page: w.page })) };
  await hide();
  out.after = wins.map(w => ({ destroyed: w.destroyed, pageLeft: fs.existsSync(w.page) }));
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(not NODE, reason="node required")
def test_the_frozen_frame_covers_every_output_at_its_own_offset_and_is_taken_down_completely(tmp_path):
    src = tmp_path / "PosterChan-freeze-1.png"
    src.write_bytes(b"x")
    js = FREEZE_JS % {"mod": json.dumps(str(FREEZE)), "src": json.dumps(str(src)), "layout": json.dumps(LAYOUT)}
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout)
    assert len(out["wins"]) == 2, "one still picture per output"
    places = sorted([c for c in out["calls"] if c[0] == "place"], key=lambda c: c[2])
    assert [c[2:] for c in places] == [[0, 0, 300, 200], [300, 50, 200, 150]], "not placed on its output"
    assert len([c for c in out["calls"] if c[0] == "top"]) == 2
    assert len([c for c in out["calls"] if c[0] == "reveal"]) == 2, "placed but never revealed"
    for w in out["wins"]:
        assert w["focusable"] is False and w["top"] is True and w["shown"] is True
        assert src.as_uri() in w["html"]
        assert "opacity:0" in w["html"], "the picture must be invisible until it sits on its output"
    by_w = {w["w"]: w["html"] for w in out["wins"]}
    # eDP-1 (300x200 of a 500x200 box at 0,0): picture is 500/300 viewport widths, no offset.
    assert "left:0vw;top:0vh;width:166.666667vw;height:100vh" in by_w[300], by_w[300]
    # DP-1 (200x150 at 300,50): shifted left by 300/200 and up by 50/150 of its own viewport.
    assert "left:-150vw;top:-33.333333vh;width:250vw;height:133.333333vh" in by_w[200], by_w[200]
    assert out["after"] == [{"destroyed": True, "pageLeft": False}] * 2, "the overlay outlived the selection"


# ── pc-screenshot (the compositor key with no desktop running) ─────────────────────────────────────

PATTERN_GRIM = r'''
import os, sys
from PIL import Image
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("grim " + " ".join(args) + "\n")
if "-g" in args:
    Image.new("RGB", (5, 5), (1, 2, 3)).save(args[-1], "PNG")
    sys.exit(0)
w, h = int(os.environ["STUB_W"]), int(os.environ["STUB_H"])
img = Image.new("RGB", (w, h))
img.putdata([(x % 256, y % 256, (x // 256) * 16 + y // 256) for y in range(h) for x in range(w)])
img.save(args[-1], "PNG")
'''


def _script_world(tmp_path, randr, slurp_out="350,60 100x80", slurp_rc=0, size=(1000, 400)):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    log = tmp_path / "calls.log"
    _stub(bin_, "grim", PATTERN_GRIM, shebang="#!" + shutil.which("python3"))
    _stub(bin_, "slurp", f'echo slurp >>"{log}"\n' + (f'echo "{slurp_out}"\n' if slurp_rc == 0 else "exit 1\n"))
    (tmp_path / "randr.json").write_text(json.dumps(randr))
    _stub(bin_, "wlr-randr", f'echo wlr-randr >>"{log}"\ncat "{tmp_path / "randr.json"}"\n')
    _stub(bin_, "pc-wayfire-action", "exit 1\n")          # no desktop to ask
    _stub(bin_, "notify-send", "exit 0\n")
    _stub(bin_, "wl-copy", "cat >/dev/null\n")
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(run),
           "UID": str(os.getuid()), "STUB_LOG": str(log), "STUB_W": str(size[0]), "STUB_H": str(size[1])}
    return env, log, run


def _run_script(tmp_path, mode, **kw):
    env, log, run = _script_world(tmp_path, kw.pop("randr", RANDR), **kw)
    r = subprocess.run(["sh", str(SCRIPT), mode], env=env, capture_output=True, text=True, timeout=60)
    shots = sorted((tmp_path / "Pictures" / "Screenshots").glob("*.png")) \
        if (tmp_path / "Pictures" / "Screenshots").exists() else []
    return r, log.read_text().splitlines() if log.exists() else [], shots, sorted(p.name for p in run.iterdir())


@pytest.mark.skipif(Image is None or not shutil.which("python3"), reason="python3 + Pillow required")
def test_the_key_freezes_first_and_crops_the_frame_at_the_right_scale(tmp_path):
    r, calls, shots, left = _run_script(tmp_path, "region")
    assert r.returncode == 0, r.stderr
    assert [c.split()[0] for c in calls] == ["grim", "slurp", "wlr-randr"], calls
    assert "-g" not in calls[0], "a second, live capture was taken instead of cropping the frozen one"
    assert len(shots) == 1
    img = Image.open(shots[0])
    # logical 350,60 100x80 on the scale-2 picture -> pixels (700,120) 200x160
    assert img.size == (200, 160)
    assert img.getpixel((0, 0)) == (700 % 256, 120, (700 // 256) * 16 + 0)
    assert img.getpixel((199, 159)) == (899 % 256, 279 % 256, (899 // 256) * 16 + 279 // 256)
    assert left == [], "the frozen frame was left in the runtime dir"


@pytest.mark.skipif(Image is None or not shutil.which("python3"), reason="python3 + Pillow required")
def test_the_key_handles_a_monitor_left_of_the_origin(tmp_path):
    randr = [
        {"name": "DP-2", "enabled": True, "scale": 1.0, "transform": "normal", "position": {"x": -400, "y": 0},
         "modes": [{"width": 400, "height": 300, "current": True}]},
        {"name": "eDP-1", "enabled": True, "scale": 2.0, "transform": "normal", "position": {"x": 0, "y": 0},
         "modes": [{"width": 1200, "height": 600, "current": True}]},
    ]
    r, calls, shots, left = _run_script(tmp_path, "region", randr=randr, slurp_out="-390,10 100x50", size=(2000, 600))
    assert r.returncode == 0, r.stderr
    img = Image.open(shots[0])
    assert img.size == (200, 100)
    assert img.getpixel((0, 0)) == (20, 20, 0)


@pytest.mark.skipif(Image is None or not shutil.which("python3"), reason="python3 + Pillow required")
def test_the_key_cancelled_leaves_no_frame_and_no_screenshot(tmp_path):
    r, calls, shots, left = _run_script(tmp_path, "region", slurp_rc=1)
    assert r.returncode == 0, r.stderr
    assert [c.split()[0] for c in calls] == ["grim", "slurp"], calls
    assert shots == [] and left == [], (shots, left)


@pytest.mark.skipif(Image is None or not shutil.which("python3"), reason="python3 + Pillow required")
def test_the_key_with_a_layout_grim_did_not_photograph_falls_back_to_a_live_capture(tmp_path):
    r, calls, shots, left = _run_script(tmp_path, "region", size=(1000, 1000))
    assert r.returncode == 0, r.stderr
    assert calls[-1] == "grim -g 350,60 100x80 " + str(shots[0]), calls
    assert Image.open(shots[0]).size == (5, 5)
    assert left == []


@pytest.mark.skipif(Image is None or not shutil.which("python3"), reason="python3 + Pillow required")
def test_the_full_screen_key_is_unchanged(tmp_path):
    r, calls, shots, left = _run_script(tmp_path, "screen")
    assert r.returncode == 0, r.stderr
    assert calls == ["grim " + str(shots[0])], calls
    assert Image.open(shots[0]).size == (1000, 400)


# ── the Print Screen prompt hands its frame to the region picker ───────────────────────────────────

def test_select_region_in_the_prompt_reuses_the_keypress_frame():
    """The prompt's "Select region" (no delay) must hand its staged frame over rather than delete it,
    and the desktop must pass it through to the capture."""
    osshell = (ROOT / "static/js/client/osshell.js").read_text()
    osjs = (ROOT / "static/js/client/os.js").read_text()
    main = (ROOT / "desktop/main.js").read_text()
    assert "shot:region-frozen:" in osshell
    assert "takeShot('region', { frozen: st.path })" in osshell
    assert "/^region-frozen:(.+)$/" in osjs and "takeShot('region', { frozen: f[1] })" in osjs
    assert "frozenRegionOpts(o)" in main and "frozen: o.frozen" in main
