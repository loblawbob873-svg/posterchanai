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
const rows = [{ id: 1, title: 'PosterChan Desktop', rect: { x: 0, y: 0, width: 3840, height: 2560 } }];
const wmApi = {
  outputs: async () => [{ name: 'DP-1', rect: { x: 0, y: 0, width: 3840, height: 2560 } },
                        { name: 'DP-2', rect: { x: 3840, y: 0, width: 3840, height: 2560 } }],
  windows: async () => rows,
  place: async (id, x, y, w, h) => calls.push(['place', id, x, y, w, h]),
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
  setSize(w, h){ calls.push(['size', w, h]); }
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
    assert not {"FOCUS", "FOCUS-ELECTRON", "SHOW-ACTIVE"} & set(names), "she took the keyboard: %r" % r["calls1"]
    # Placed on the asking renderer's output (DP-2 starts at x=3840), scaled 2x from its viewport.
    assert ["place", 42, 3840 + 3000, 1800, 348, 568] in r["calls1"], r["calls1"]


def test_only_her_own_window_can_move_or_hide_her_and_the_spot_goes_back_to_be_saved():
    r = _run()
    assert r["dragStranger"] is False and r["hideStranger"] is False, r
    # Her page reports CSS px; the window is 2x in the compositor.
    assert r["drag"] is True and r["dragCalls"] == [["move", 42, 6860, 1790]], r["dragCalls"]
    assert r["sent"] == [["pc:buddy:event", {"type": "moved", "vx": 1510, "vy": 895}]], r["sent"]
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
