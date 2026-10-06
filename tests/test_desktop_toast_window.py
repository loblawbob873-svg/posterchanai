"""Notification cards on PosterChanOS: their own window, over every application window.

"desktop missed another social notification" / "i am not seeing any notifications for telegram
messages": the desktop drew its cards inside the desktop surface, which the compositor keeps under every
window, and osNotify used Electron's native notifications, which need a notification server this OS
deliberately does not run. Runs the SHIPPED desktop/toast-host.js under node against a fake compositor.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
const { createToastHost, TITLE } = require(%(host)s);
const calls = [];
const rows = [{ id: 1, title: 'PosterChan Desktop', rect: { x: 0, y: 0, width: 3840, height: 2560 }, focusTime: 5 },
              { id: 9, title: 'PosterChan Window — terminal', rect: { x: 0, y: 0, width: 800, height: 600 }, focusTime: 40 }];
const wmApi = {
  outputs: async () => [{ name: 'DP-1', rect: { x: 0, y: 0, width: 3840, height: 2560 } },
                        { name: 'DP-2', rect: { x: 3840, y: 0, width: 3840, height: 2560 },
                          work: { x: 3840, y: 0, w: 3840, h: 2484 } }],
  windows: async () => rows,
  place: async (id, x, y, w, h) => { calls.push(['place', id, x, y, w, h]); },
  alwaysOnTop: async (id, on) => calls.push(['top', id, on]),
  sticky: async (id, on) => calls.push(['sticky', id, on]),
  focus: async id => calls.push(['FOCUS', id]),
};
let made = null, clock = 1000, nextView = 50;
const timers = [];
class FakeWin {
  constructor(o){ made = this; this.o = o; this.dead = false; this.visible = false; this.webContents = { id: 77, sent: [], send(ch, v){ this.sent.push([ch, v]); } }; this.ev = {}; }
  on(n, f){ this.ev[n] = f; }
  async loadFile(p){ this.page = p; }
  // LIKE WAYFIRE: hiding a window UNMAPS it, and showing it again maps a NEW view with a NEW id. The fake
  // used to keep id 50 for ever -- it agreed with the bug, so every card after the first could be placed
  // on a view that no longer existed and the test still passed.
  showInactive(){ if(this.visible) return; this.visible = true; calls.push(['showInactive']); rows.push({ id: nextView++, title: TITLE, rect: { x: 0, y: 0, width: this.o.width * 2, height: this.o.height * 2 } }); }
  show(){ calls.push(['SHOW-ACTIVE']); }
  hide(){ this.visible = false; calls.push(['hide']); for(let i = rows.length - 1; i >= 0; i--) if(rows[i].title === TITLE) rows.splice(i, 1); }
  isVisible(){ return this.visible; }
  getBounds(){ return { width: this.o.width, height: this.o.height }; }
  setSize(w, h){ this.o.width = w; this.o.height = h; }
  isDestroyed(){ return this.dead; }
}
const owner = { id: 7, sent: [], isDestroyed: () => false, send(ch, ev){ this.sent.push([ch, ev]); } };
const host = createToastHost({ BrowserWindow: FakeWin, wm: () => wmApi, scopeOf: id => id === 7 ? { output: 'DP-2' } : null,
  pagePath: '/x/toast.html', preloadPath: '/x/toast-preload.js', sleep: async () => {},
  now: () => clock, setTimeout: (f, ms) => { timers.push([clock + ms, f]); return timers.length; } });
(async () => {
  const out = {};
  out.shown = await host.show(owner, { id: 'a', html: '<b>Telegram · Bob</b><br>hi', pic: '' });
  out.opts = { focusable: made.o.focusable, alwaysOnTop: made.o.alwaysOnTop, transparent: made.o.transparent,
               title: made.o.title, sandbox: made.o.webPreferences.sandbox, page: made.page };
  out.calls1 = calls.splice(0);
  out.cards1 = made.webContents.sent.slice(-1)[0];
  // A stranger cannot click a card; the card window can, and the raising page is told which.
  out.stranger = host.click({ id: 8 }, 'a', false);
  await host.show(owner, { id: 'b', html: 'second', pic: 'https://x/y.png' });
  calls.splice(0);
  out.clicked = host.click(made.webContents, 'a', false);
  out.ownerSent = owner.sent.splice(0);
  out.dismissed = host.click(made.webContents, 'b', true);
  out.ownerSentAfterDismiss = owner.sent.splice(0);
  await new Promise(r => setTimeout(r, 5));
  out.afterAllGone = calls.splice(0);
  // Expiry: a card raised and left alone goes after its time, and the window hides when empty.
  await host.show(owner, { id: 'c', html: 'third' });
  calls.splice(0);
  clock += 8000;
  for(const [, f] of timers.splice(0)) f();
  await new Promise(r => setTimeout(r, 5));
  out.afterExpiry = { calls: calls.splice(0), state: host._state() };
  // The NEXT notification maps the window again -- as a new view -- and must still go to the corner,
  // on top, on every workspace. "toast notifications are in the center of my screen".
  await host.show(owner, { id: 'd', html: 'fourth' });
  out.reshown = { calls: calls.splice(0), view: rows.filter(r => r.title === TITLE).map(r => r.id) };
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _run():
    r = subprocess.run(["node", "-e", SCRIPT % {"host": json.dumps(str(ROOT / "desktop/toast-host.js"))}],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_card_gets_its_own_unfocusable_window_over_every_application():
    d = _run()
    assert d["shown"] is True
    assert d["opts"]["focusable"] is False and d["opts"]["alwaysOnTop"] is True and d["opts"]["transparent"] is True, d["opts"]
    assert d["opts"]["sandbox"] is True and d["opts"]["page"].endswith("toast.html"), d["opts"]
    names = [c[0] for c in d["calls1"]]
    assert ["top", 50, True] in d["calls1"] and ["sticky", 50, True] in d["calls1"], d["calls1"]
    assert "SHOW-ACTIVE" not in names, "a card must never take the keyboard"
    assert ["FOCUS", 9] in d["calls1"], ("focus was not handed back to the window that had it", d["calls1"])


def test_it_sits_bottom_right_of_the_raising_monitor_above_the_taskbar():
    d = _run()
    place = [c for c in d["calls1"] if c[0] == "place"][-1]
    _, _, x, y, w, h = place
    assert 3840 <= x and x + w <= 3840 * 2, ("not on DP-2, the monitor that raised it", place)
    assert x + w > 3840 * 2 - 100, ("not in the right-hand corner", place)
    assert y + h <= 2484 and y + h > 2484 - 100, ("not just above the taskbar (work area ends at 2484)", place)


def test_a_click_goes_back_to_the_page_that_raised_it_and_only_from_the_card_window():
    d = _run()
    assert d["stranger"] is False
    assert d["clicked"] is True and d["ownerSent"] == [["pc:toast:clicked", "a"]], d["ownerSent"]
    assert d["dismissed"] is True and d["ownerSentAfterDismiss"] == [], "a dismissed card must not act"
    assert ["hide"] in d["afterAllGone"], ("the empty window stayed on screen", d["afterAllGone"])


def test_a_card_expires_and_the_window_hides():
    d = _run()
    assert d["afterExpiry"]["state"]["cards"] == [] and ["hide"] in d["afterExpiry"]["calls"], d["afterExpiry"]


def test_the_next_card_after_the_window_hid_still_goes_to_the_corner():
    """Wayfire maps a hidden window back as a NEW view. Placing the old id moved nothing, and a freshly
    mapped window sits wherever Wayfire puts it: the middle of the screen."""
    d = _run()
    calls, view = d["reshown"]["calls"], d["reshown"]["view"]
    assert len(view) == 1 and view[0] != 50, ("the fake did not re-map the window as a new view", view)
    new = view[0]
    places = [c for c in calls if c[0] == "place"]
    assert places and all(c[1] == new for c in places), ("placed a view that no longer exists", calls)
    _, _, x, y, w, h = places[-1]
    assert x + w > 3840 * 2 - 100 and y + h > 2484 - 100, ("not in the bottom-right corner", places[-1])
    assert ["top", new, True] in calls and ["sticky", new, True] in calls, ("the new view is not kept on top", calls)
