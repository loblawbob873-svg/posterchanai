/* PRESS THE TRAY CHIP, AND ASK WHETHER A FLYOUT IS ON SCREEN AFTERWARDS.
 *
 * Runs the SHIPPED osshell.js against a model of desktop/main.js's popup host. The model carries
 * the one behaviour that cannot be seen from inside the client and that had to be measured on the
 * machine: a popup window created to REPLACE one that is still up loses the focus race and is
 * destroyed before it is ever placed or revealed. Reproduced in an isolated Wayfire session by
 * pressing the chip N times and then asking the compositor what is mapped —
 *
 *     1 press  → 'PosterChan Popup' 360x446, painted, the volume slider live
 *     2 presses → two popup windows created, NOTHING mapped
 *     3 presses → the flyout again
 *     4 presses → nothing
 *
 * — which is "the volume widget is not even functional". Both `open` and `toggle` are exposed here
 * on purpose: the client must CHOOSE the one that never re-enters the create path, and a fixture
 * that simply lacked `open` would pass for the wrong reason.
 */
'use strict';
const path = require('path');
const MOD = path.join(__dirname, '..', '..', 'static', 'js', 'client', 'osshell.js');

/* A DOM small enough to read — the same shape test_os_shell_render.py uses: innerHTML records the
   buttons it was handed and querySelectorAll returns them, so handlers can be attached and fired. */
function el() {
  const e = { _html: '', children: [], dataset: {}, disabled: false, onclick: null };
  Object.defineProperty(e, 'innerHTML', {
    get() { return e._html; },
    set(v) {
      e._html = String(v);
      e.children = [];
      const re = /<button[^>]*?(?:data-(app|win|os)="([^"]*)")[^>]*>([\s\S]*?)<\/button>/g;
      let m;
      while ((m = re.exec(e._html))) {
        const b = { dataset: {}, disabled: false, onclick: null, text: m[3] };
        b.dataset[m[1] === 'win' ? 'win' : (m[1] === 'app' ? 'app' : 'os')] = m[2];
        b.getBoundingClientRect = () => ({ left: 900, top: 700, right: 980, bottom: 740,
                                           width: 80, height: 40 });
        e.children.push(b);
      }
    },
  });
  e.querySelectorAll = (sel) => {
    const key = sel.indexOf('data-app') >= 0 ? 'app' : sel.indexOf('data-win') >= 0 ? 'win' : 'os';
    return e.children.filter(c => c.dataset[key] !== undefined);
  };
  e.appendChild = () => {};
  return e;
}

const host = { live: null, kind: '', created: 0, calls: [], openWhileLive: 0 };
function closeWin() { host.live = null; host.kind = ''; }
function openWin(kind) {
  const replacing = !!host.live;          // the previous surface is still mapped
  closeWin();                             // main.js destroys it first
  host.created += 1;
  host.live = replacing ? null : { kind }; // …and the replacement dies before it is placed
  host.kind = host.live ? kind : '';
  return !!host.live;
}

globalThis.pcPopup = {
  open: (kind) => { host.calls.push('open:' + kind); if (host.live) host.openWhileLive += 1;
                    return Promise.resolve(openWin(kind)); },
  toggle: (kind) => {
    host.calls.push('toggle:' + kind);
    if (host.live && host.kind === kind) { closeWin(); return Promise.resolve(false); }
    if (host.live) host.openWhileLive += 1;
    return Promise.resolve(openWin(kind));
  },
  close: () => { closeWin(); return Promise.resolve(true); },
};

globalThis.pcWM = {
  windows: async () => [],
  focus: async () => true,
  launch: async () => ({ pid: 1, window: null }),
  subscribe: async () => true,
  onEvent: () => () => {},
};
globalThis.pcNet = { status: async () => ({ online: true, kind: 'ethernet', name: 'Wired' }) };
globalThis.pcPower = { status: async () => ({ battery: { present: false }, brightness: { available: false },
                                              profiles: { available: true, kind: 'platform',
                                                          list: ['powersave'], active: 'powersave' },
                                              canHibernate: true }) };
globalThis.pcAudio = { status: async () => ({ output: { percent: 60, muted: false } }) };
globalThis.innerWidth = 1280;
globalThis.innerHeight = 800;

const S = require(MOD);

(async () => {
  const bar = el();
  await S.refresh();
  const presses = Number(process.argv[2] || 1);
  const mapped = [];
  for (let i = 0; i < presses; i++) {
    S.paintTray(bar);                      // the taskbar is repainted on every shell tick
    const chip = bar.querySelectorAll('[data-os]').find(b => b.dataset.os === 'quick');
    if (!chip) { process.stdout.write(JSON.stringify({ error: 'no tray chip was rendered' })); return; }
    chip.onclick({ stopPropagation() {} });
    await new Promise(r => setTimeout(r, 0));
    mapped.push(!!host.live);
  }
  process.stdout.write(JSON.stringify({
    mapped, created: host.created, calls: host.calls, openWhileLive: host.openWhileLive,
  }));
})();
