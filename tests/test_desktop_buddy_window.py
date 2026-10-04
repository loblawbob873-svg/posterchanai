"""The desktop PosterChan on PosterChanOS: her own window, kept over every other window.

"posterchan should be going over windows right? should never be hidden unless you kill it". On
PosterChanOS the apps are real compositor windows and the desktop surface is under all of them, so
she gets a window of her own (desktop/buddy-host.js). Runs the SHIPPED host under node against a
fake compositor and a fake Electron window whose focus calls are recorded.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
const { createBuddyHost, TITLE } = require(%(host)s);
const calls = [];
const rows = [{ id: 1, title: 'PosterChan Desktop', rect: { x: 0, y: 0, width: 3840, height: 2560 }, focusTime: 5 },
              { id: 9, title: 'PosterChan Window — terminal', rect: { x: 0, y: 0, width: 800, height: 600 }, focusTime: 40 },
              { id: 12, title: 'Firefox', rect: { x: 0, y: 0, width: 800, height: 600 }, focusTime: 20 }];
const wmApi = {
  outputs: async () => [{ name: 'DP-1', rect: { x: 0, y: 0, width: 3840, height: 2560 } },
                        { name: 'DP-2', rect: { x: 3840, y: 0, width: 3840, height: 2560 } }],
  windows: async () => rows,
  // As Wayfire: placing a window gives it that size (a real compositor resize).
  place: async (id, x, y, w, h) => { calls.push(['place', id, x, y, w, h]);
    const r = rows.find(v => v.id === id); if(r) r.rect = { x, y, width: w, height: h }; },
  move: async (id, x, y) => calls.push(['move', id, x, y]),
  alwaysOnTop: async (id, on) => calls.push(['top', id, on]),
  sticky: async (id, on) => calls.push(['sticky', id, on]),
  focus: async id => calls.push(['FOCUS', id]),
};
let made = null;
class FakeWin {
  constructor(o){ made = this; this.o = o; this.dead = false; this.webContents = { id: 99 }; this.ev = {}; }
  on(n, f){ this.ev[n] = f; }
  async loadFile(p, o){ this.page = p; (this.loads = this.loads || []).push((o && o.hash) || ''); }
  showInactive(){ calls.push(['showInactive']); rows.push({ id: 42, title: TITLE, rect: { x: 0, y: 0, width: this.o.width * 2, height: this.o.height * 2 } }); }
  show(){ calls.push(['SHOW-ACTIVE']); }
  focus(){ calls.push(['FOCUS-ELECTRON']); }
  getBounds(){ return { width: this.o.width, height: this.o.height }; }
  // As Electron on Wayland: a non-resizable window keeps its first size whatever is asked later.
  setSize(w, h){ if (this.o.resizable === false) return; this.o.width = w; this.o.height = h; calls.push(['size', w, h]); }
  isDestroyed(){ return this.dead; }
  destroy(){ this.dead = true; calls.push(['destroy']); }
}
const owner = { id: 7, sent: [], isDestroyed: () => false, send(ch, ev){ this.sent.push([ch, ev]); } };
const stranger = { id: 8 };
const host = createBuddyHost({ BrowserWindow: FakeWin, wm: () => wmApi, scopeOf: id => id === 7 ? { output: 'DP-2' } : null,
                               pagePath: '/x/buddy.html', preloadPath: '/x/buddy-preload.js', sleep: async () => {} });
(async () => {
  const out = {};
  // The DP-2 desktop renderer: viewport 1920x1280 CSS px on a 3840x2560 output (2x).
  out.shown = await host.show(owner, { vx: 1500, vy: 900, bw: 174, bh: 284, vw: 1920, vh: 1280 });
  out.opts = { frame: made.o.frame, transparent: made.o.transparent, focusable: made.o.focusable, skipTaskbar: made.o.skipTaskbar,
               title: made.o.title, sandbox: made.o.webPreferences.sandbox, preload: made.o.webPreferences.preload, page: made.page };
  out.calls1 = calls.splice(0);
  // A later show with a SMALLER box (the desktop reached its real size) must actually resize her.
  await host.show(owner, { vx: 1500, vy: 900, bw: 87, bh: 142, vw: 1920, vh: 1280 });
  out.resized = { w: made.o.width, h: made.o.height };
  calls.splice(0);
  // Her window is activated (a click): focus goes back to the window that had it -- the terminal.
  rows.find(r => r.title === TITLE).focusTime = 99;
  made.ev.focus();
  await new Promise(r => setTimeout(r, 10));
  out.focusBack = calls.splice(0);
  out.dragStranger = host.drag(stranger, 50, 0);
  out.drag = host.drag(made.webContents, 10, -5);
  out.dragCalls = calls.splice(0);
  host.drop(made.webContents);
  out.sent = owner.sent.splice(0);
  out.switchStranger = host.menu(stranger, 'switch');
  out.switched = host.menu(made.webContents, 'switch');
  out.sentSwitch = owner.sent.splice(0);
  out.openAfterSwitch = host._state().open;
  out.hideStranger = host.menu(stranger, 'hide');
  out.hide = host.menu(made.webContents, 'hide');
  out.sent2 = owner.sent.splice(0);
  out.afterHide = calls.splice(0);
  await host.show(owner, { vx: 0, vy: 0, bw: 174, bh: 284, vw: 1920, vh: 1280 });
  calls.splice(0);
  host.ownerGone(7);
  out.ownerGone = calls.splice(0);
  // Who dances: the page is told, and a switch reloads her SAME window for the other one.
  await host.show(owner, { vx: 0, vy: 0, bw: 174, bh: 284, vw: 1920, vh: 1280, who: 'axolotl' });
  const first = made;
  await host.show(owner, { vx: 0, vy: 0, bw: 174, bh: 284, vw: 1920, vh: 1280, who: 'posterchan' });
  out.who = { loads: made.loads, sameWindow: made === first,
              bad: (await host.show(owner, { vx: 0, vy: 0, bw: 174, bh: 284, vw: 1920, vh: 1280, who: '../x' }), made.loads.slice(-1)[0]) };
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _run():
    script = SCRIPT % {"host": json.dumps(str(ROOT / "desktop/buddy-host.js"))}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_she_gets_a_window_pinned_over_every_other_and_never_takes_focus():
    r = _run()
    assert r["shown"] is True, r
    o = r["opts"]
    assert o["frame"] is False and o["transparent"] is True and o["skipTaskbar"] is True, o
    assert o["focusable"] is False and o["title"] == "PosterChan Buddy" and o["sandbox"] is True, o
    assert o["preload"].endswith("buddy-preload.js") and o["page"].endswith("buddy.html"), o
    names = [c[0] for c in r["calls1"]]
    assert "showInactive" in names, r["calls1"]
    assert ["top", 42, True] in r["calls1"], "not kept on top of every window: %r" % r["calls1"]
    assert ["sticky", 42, True] in r["calls1"], "not on every workspace: %r" % r["calls1"]
    assert not {"FOCUS-ELECTRON", "SHOW-ACTIVE"} & set(names) and ["FOCUS", 42] not in r["calls1"], \
        "she took the keyboard: %r" % r["calls1"]
    # Wayfire activates every window it maps: the window that had the keyboard (the terminal) gets it back.
    assert ["FOCUS", 9] in r["calls1"], "she kept the keyboard she was given on appearing: %r" % r["calls1"]
    # Placed on the asking renderer's output (DP-2 starts at x=3840), scaled 2x from its viewport.
    assert ["place", 42, 3840 + 3000, 1800, 348, 568] in r["calls1"], r["calls1"]


def test_only_her_own_window_can_move_or_hide_her_and_the_spot_goes_back_to_be_saved():
    r = _run()
    assert r["dragStranger"] is False and r["hideStranger"] is False, r
    # Her page reports CSS px; the window is 2x in the compositor.
    assert r["drag"] is True and r["dragCalls"] == [["move", 42, 6860, 1790]], r["dragCalls"]
    assert len(r["sent"]) == 1 and r["sent"][0][0] == "pc:buddy:event", r["sent"]
    ev = r["sent"][0][1]
    assert {k: ev[k] for k in ("type", "vx", "vy")} == {"type": "moved", "vx": 1510, "vy": 895}, ev
    assert ev["out"] == "DP-2", ev                       # and the monitor she was left on
    assert r["hide"] is True and r["sent2"] == [["pc:buddy:event", {"type": "hide"}]], r
    assert r["switchStranger"] is False and r["switched"] is True and r["openAfterSwitch"] is True, r
    assert r["sentSwitch"] == [["pc:buddy:event", {"type": "switch"}]], r
    assert ["destroy"] in r["afterHide"], r["afterHide"]
    assert ["destroy"] in r["ownerGone"], "she outlived the desktop that owns her: %r" % r["ownerGone"]


def test_the_chosen_dancer_is_drawn_and_a_switch_reloads_her_same_window():
    """'make an alternative to posterchan that users can choose, a dancing axolotl'."""
    r = _run()["who"]
    assert r["sameWindow"], "switching dancer opened a second window"
    assert r["loads"] == ["axolotl", "posterchan"], r
    assert r["bad"] == "posterchan", "a junk dancer id must fall back, not reach her page's address"


def test_main_wires_her_host_to_the_window_class_electron_assigns_later():
    """On the real machine every show() threw 'BrowserWindow is not a constructor': main.js assigns
    BrowserWindow once Electron is ready, AFTER the host is created. Runs main.js's own wiring with that
    ordering."""
    main = (ROOT / "desktop/main.js").read_text()
    wiring = main[main.index("const buddyHost = require('./buddy-host.js')"):main.index("ipcMain.handle('pc:buddy:show'")]
    script = """
      const path = require('path'); const __dirname = %s;
      let BrowserWindow;                                    // assigned later, as in main.js
      const _shellScopes = new Map([[7, { output: 'A' }]]);
      const made = [];
      const wm = () => ({ outputs: async () => [{ name: 'A', rect: { x: 0, y: 0, width: 100, height: 100 } }],
                          windows: async () => made.length ? [{ id: 1, title: 'PosterChan Buddy', rect: { width: 10 } }] : [],
                          place: async () => {}, alwaysOnTop: async () => {}, sticky: async () => {} });
      %s
      BrowserWindow = class { constructor(o){ made.push(o.title); this.webContents = {}; }
        on(){} async loadFile(){} showInactive(){} getBounds(){ return { width: 10 }; } setSize(){}
        isDestroyed(){ return false; } };
      buddyHost.show({ id: 7, isDestroyed: () => false, send(){} }, { vx: 1, vy: 1, bw: 10, bh: 10, vw: 100, vh: 100 })
        .then(ok => console.log(JSON.stringify({ ok, made })), e => { console.log(JSON.stringify({ error: String(e) })); });
    """ % (json.dumps(str(ROOT / "desktop")), wiring)
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, cwd=ROOT / "desktop")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out.get("ok") is True and out.get("made") == ["PosterChan Buddy"], out


def test_a_click_on_her_never_keeps_the_keyboard():
    """'fix the focus issue too': clicking her made her window the focused one, so typing went nowhere."""
    r = _run()
    assert r["focusBack"] == [["FOCUS", 9]], ("focus did not go back to the window that had it", r["focusBack"])


def test_a_later_smaller_size_really_resizes_her_window():
    """Measured: created while the desktop was 500x540 she stayed 668x568 for good (min = max size),
    off the right edge and unable to reach it. A later show must shrink the window itself."""
    r = _run()
    assert r["resized"] == {"w": 87, "h": 142}, ("her window kept its first size", r["resized"])


EDGES = r"""
const { createBuddyHost, TITLE } = require(%(host)s);
const calls = [];
let stuck = %(stuck)s;          // the compositor keeps her at this size whatever is asked
const rows = [{ id: 1, title: 'PosterChan Desktop', rect: { x: 0, y: 0, width: 3840, height: 2560 } }];
const wmApi = {
  outputs: async () => [{ name: 'DP-1', rect: { x: 0, y: 0, width: 3840, height: 2560 } },
                        { name: 'DP-2', rect: { x: 3840, y: 0, width: 3840, height: 2560 } }],
  windows: async () => rows,
  place: async (id, x, y, w, h) => { calls.push(['place', id, x, y, w, h]);
    const r = rows.find(v => v.id === id); if(r) r.rect = stuck ? { x, y, width: stuck[0], height: stuck[1] } : { x, y, width: w, height: h }; },
  move: async (id, x, y) => calls.push(['move', id, x, y]),
  alwaysOnTop: async () => {}, sticky: async () => {}, focus: async () => {},
};
let made = null, built = 0;
class FakeWin {
  constructor(o){ made = this; built++; this.o = o; this.dead = false; this.webContents = { id: 90 + built }; this.ev = {}; }
  on(n, f){ this.ev[n] = f; }
  async loadFile(){}
  showInactive(){ rows.push({ id: 40 + built, title: TITLE, rect: { x: 0, y: 0, width: this.o.width, height: this.o.height } }); }
  getBounds(){ return { width: this.o.width, height: this.o.height }; }
  setSize(w, h){ this.o.width = w; this.o.height = h; }
  isDestroyed(){ return this.dead; }
  destroy(){ this.dead = true; const i = rows.findIndex(r => r.id === 40 + built); if(i >= 0) rows.splice(i, 1); calls.push(['destroy']); }
}
const owner = { id: 7, sent: [], isDestroyed: () => false, send(ch, ev){ this.sent.push([ch, ev]); } };
const host = createBuddyHost({ BrowserWindow: FakeWin, wm: () => wmApi, scopeOf: () => ({ output: 'DP-2' }),
                               pagePath: '/x/buddy.html', preloadPath: '/x/b.js', sleep: async () => {} });
(async () => {
  const out = {};
  // Asked for at the bottom-right of DP-2 (the spot the account remembered).
  out.shown = await host.show(owner, { vx: 1900, vy: 1250, bw: 174, bh: 284, vw: 1920, vh: 1280 });
  out.built = built;
  const me = rows.find(r => r.title === TITLE);
  out.rect = me && me.rect;
  out.state = { ...host._state().at };
  // Drag hard right: she must stop with her right edge ON the screen's edge.
  host.drag(made.webContents, 5000, 0);
  out.right = { ...host._state().at };
  // Drag left across onto DP-1: she must cross, not stop at DP-2's left edge.
  host.drag(made.webContents, -4000, 0);
  out.left = { ...host._state().at };
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _edges(stuck="null"):
    script = EDGES % {"host": json.dumps(str(ROOT / "desktop/buddy-host.js")), "stuck": stuck}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _on_one_output(a):
    for x0 in (0, 3840):
        if a["x"] >= x0 and a["x"] + a["w"] <= x0 + 3840 and a["y"] >= 0 and a["y"] + a["h"] <= 2560:
            return True
    return False


def test_she_can_be_dragged_onto_the_other_monitor_and_right_up_to_the_edge():
    """'when you drag it to the other monitor, it goes about 15% in' / 'you can't drag her all the way
    to the right': the drag was clamped to the monitor she started on, with the size asked for."""
    r = _edges()
    right = r["right"]
    assert right["x"] + right["w"] == 7680, right          # flush with the right edge, not short of it
    left = r["left"]
    assert left["x"] < 3840 and left["x"] + left["w"] <= 3840, left   # crossed onto DP-1, wholly on it
    assert _on_one_output(left) and _on_one_output(right), r


def test_a_window_that_refused_its_size_is_rebuilt_and_never_left_off_the_screen():
    """'it's still starting off off screen partially' / 'on my TV, posterchan is mostly off the screen':
    measured 1337x1347 at y=1138 on a 2560-high desk -- clamped with the size ASKED, not the size she had."""
    r = _edges(stuck="[1337, 1347]")
    assert r["built"] == 2, "an oversized window was not rebuilt once: %r" % r
    # Still stuck after the rebuild: she is kept wholly on screen with the size she really has.
    assert r["state"]["w"] == 1337 and r["state"]["h"] == 1347, r
    assert _on_one_output(r["state"]), r
    assert _on_one_output(r["right"]) and _on_one_output(r["left"]), r


def test_a_window_of_the_right_size_is_built_once_and_starts_wholly_on_screen():
    r = _edges()
    assert r["built"] == 1, r
    assert _on_one_output(r["state"]), r


PERSIST = r"""
const { createBuddyHost, TITLE } = require(%(host)s);
const calls = [];
const rows = [{ id: 1, title: 'PosterChan Desktop', rect: { x: 0, y: 0, width: 3840, height: 2560 }, focusTime: 5 },
              { id: 9, title: 'Terminal', rect: { x: 0, y: 0, width: 800, height: 600 }, focusTime: 40 }];
const wmApi = {
  outputs: async () => [{ name: 'DP-1', rect: { x: 0, y: 0, width: 3840, height: 2560 } },
                        { name: 'DP-2', rect: { x: 3840, y: 0, width: 3840, height: 2560 } }],
  windows: async () => rows,
  place: async (id, x, y, w, h) => { calls.push(['place', id, x, y, w, h]); const r = rows.find(v => v.id === id); if(r) r.rect = { x, y, width: w, height: h }; },
  move: async (id, x, y) => calls.push(['move', id, x, y]),
  alwaysOnTop: async () => {}, sticky: async () => {}, focus: async id => calls.push(['FOCUS', id]),
};
let made = null;
class FakeWin {
  constructor(o){ made = this; this.o = o; this.dead = false; this.webContents = { id: 99 }; this.ev = {}; }
  on(n, f){ this.ev[n] = f; } async loadFile(){}
  showInactive(){ rows.push({ id: 42, title: TITLE, rect: { x: 0, y: 0, width: this.o.width, height: this.o.height }, focusTime: 1 }); }
  getBounds(){ return { width: this.o.width, height: this.o.height }; }
  setSize(w, h){ this.o.width = w; this.o.height = h; } isDestroyed(){ return this.dead; } destroy(){ this.dead = true; }
}
const owner = { id: 7, sent: [], isDestroyed: () => false, send(ch, ev){ this.sent.push(ev); } };
const host = createBuddyHost({ BrowserWindow: FakeWin, wm: () => wmApi, scopeOf: () => ({ output: 'DP-2' }),
                               pagePath: '/x/buddy.html', preloadPath: '/x/b.js', sleep: async () => {} });
(async () => {
  const out = {};
  // Saved on DP-1 (NOT the desktop's own monitor), bottom-right corner.
  await host.show(owner, { vx: 10, vy: 10, bw: 174, bh: 284, vw: 3840, vh: 2560, out: 'DP-1', fx: 1, fy: 1 });
  out.restored = { ...host._state().at };
  // A monitor that is not connected now: her spot on the desktop's own monitor.
  await host.show(owner, { vx: 10, vy: 10, bw: 174, bh: 284, vw: 3840, vh: 2560, out: 'HDMI-9', fx: 1, fy: 1 });
  out.fallback = { ...host._state().at };
  // Dragged onto DP-1 and dropped: the drop names the monitor and her spot on it.
  host.drag(made.webContents, -3000, 0);
  calls.splice(0); owner.sent.splice(0);
  host.drop(made.webContents);
  await new Promise(r => setTimeout(r, 5));
  out.dropped = owner.sent.splice(0);
  out.dropCalls = calls.splice(0);
  host.menu(made.webContents, 'bigger'); host.menu(made.webContents, 'smaller');
  out.sizes = owner.sent.splice(0);
  calls.splice(0);
  host.menu(made.webContents, 'release');
  await new Promise(r => setTimeout(r, 5));
  out.release = calls.splice(0);
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _persist():
    script = PERSIST % {"host": json.dumps(str(ROOT / "desktop/buddy-host.js"))}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_she_comes_back_on_the_monitor_and_spot_she_was_left_on():
    """'you need to make the axolotl, posterchan resizeable and persistent'."""
    r = _persist()
    a = r["restored"]
    assert a["x"] + a["w"] == 3840 and a["y"] + a["h"] == 2560, a      # DP-1's bottom-right, not DP-2's
    f = r["fallback"]
    assert f["x"] >= 3840, "an unplugged monitor must leave her on the desktop's own: %r" % f


def test_a_drop_names_the_monitor_and_spot_and_hands_the_keyboard_back():
    r = _persist()
    d = r["dropped"][0]
    assert d["type"] == "moved" and d["out"] == "DP-1", d
    assert 0 <= d["fx"] <= 1 and 0 <= d["fy"] <= 1, d
    assert ["FOCUS", 9] in r["dropCalls"], "she kept the keyboard after a drag: %r" % r["dropCalls"]
    assert ["FOCUS", 9] in r["release"], "she kept the keyboard after a click: %r" % r["release"]


def test_bigger_and_smaller_go_to_the_desktop_that_saves_her_size():
    r = _persist()
    assert r["sizes"] == [{"type": "size", "step": 1}, {"type": "size", "step": -1}], r["sizes"]
