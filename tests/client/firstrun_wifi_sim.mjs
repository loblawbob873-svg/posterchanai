/* Run the SHIPPED `stepNetwork` from osfirstrunui.js against a stubbed shell and a fake clock.
 *
 * THE QUESTION: can the card rebuild itself while somebody is typing a wifi password into it?
 *
 * Reported from a live boot — "it keeps reloading the password dialog then it reloaded the Join a
 * network screen", and "stopped responding to clicking the access point". The card polls
 * `net.status()` every two seconds and calls `run()` the moment it sees `online`; `run()` tears the
 * card down and rebuilds it, including the access-point buttons whose click handlers are bound to
 * those exact elements. A password prompt is an `await` in the middle of that, and associating with
 * an AP can report `online` before the join finishes — so the rebuild lands underneath the dialog.
 *
 * This drives the real function: click an AP, hold the prompt open (as a person typing does), fire
 * the watcher with `online: true`, and count `run()` calls. It is behavioural, not a grep — the
 * pre-fix code calls run() during the prompt and this reports it.
 *
 * argv[2] is a JSON plan: { tickWhilePrompting: n, online: bool, secure: bool, joinOk: bool }
 */
import fs from 'node:fs';

const src = fs.readFileSync(new URL('../../static/js/client/osfirstrunui.js', import.meta.url), 'utf8');
const a = src.indexOf('  async function stepNetwork(');
if (a < 0) throw new Error('stepNetwork moved');
const b = src.indexOf('\n  /* INSTANCE.', a);
if (b < 0) throw new Error('the end of stepNetwork moved');
const body = src.slice(a, b);

// The module-level flag the fix uses. Declared here because the slice does not include it; if the
// shipped file stops declaring one, `_netBusy` is simply never set and the test still measures the
// real behaviour (run() during the prompt), which is the point.
const prelude = 'let _netWatch = 0; let _netBusy = false;\n';

const plan = JSON.parse(process.argv[2] || '{}');
const log = { runs: 0, promptOpened: 0, joined: null, rebuiltDuringPrompt: 0 };

let promptPending = false;
let resolvePrompt = null;

/* A DOM small enough to be honest: the card must be able to answer querySelector for the controls
 * stepNetwork binds, and `_el.contains(card)` must stay true so the watcher does not self-cancel. */
function makeEl(attrs = {}) {
  const el = {
    dataset: attrs.dataset || {},
    disabled: false,
    innerHTML: '',
    _kids: [],
    onclick: null,
    querySelector(sel) { return this._find(sel)[0] || null; },
    querySelectorAll(sel) { return this._find(sel); },
    _find(sel) {
      const want = String(sel);
      return this._kids.filter(k => k._sel === want || (want === '[data-ssid]' && k.dataset.ssid));
    },
  };
  return el;
}

const rows = [];
const card = makeEl();
const box = makeEl();
box._sel = '#osfr-wifi';
// Rebuilding the list is what `box.innerHTML = ...` means in the shipped code; the buttons below
// stand in for what it produces, and the harness binds them the same way the code does.
Object.defineProperty(box, 'innerHTML', {
  get() { return ''; },
  set() { if (promptPending) log.rebuiltDuringPrompt++; },
});
for (const name of ['rescan', 'nonet']) {
  const btn = makeEl();
  btn._sel = `[data-fr="${name}"]`;
  card._kids.push(btn);
}
const ap = makeEl({ dataset: { ssid: 'HomeWifi', sec: plan.secure === false ? '' : '1' } });
rows.push(ap);
box._kids.push(ap);
card._kids.push(box);

const _el = { contains: () => true };

const net = {
  status: async () => ({ online: !!plan.online }),
  wifi: async () => ([{ ssid: 'HomeWifi', secure: plan.secure !== false, signal: 70, active: false }]),
  connect: async () => { log.joined = 'HomeWifi'; return { ok: plan.joinOk !== false }; },
};
const PC = () => ({
  uiPrompt: () => {
    log.promptOpened++;
    promptPending = true;
    return new Promise(res => { resolvePrompt = v => { promptPending = false; res(v); }; });
  },
});
const FR = () => ({ networksForPicker: list => list });
const shell = () => card;
const say = () => {};
const set = () => {};
const H = s => String(s);
const run = () => { log.runs++; };

// setInterval is captured so the test can fire the watcher deliberately rather than wait 2s.
let tick = null;
const setIntervalStub = fn => { tick = fn; return 1; };
const clearIntervalStub = () => { tick = null; };

// `stepNetwork` reaches the bridge through NET(), not a bound `net` — matched to the shipped names.
const NET = () => net;
const fn = new Function(
  'shell', 'NET', 'PC', 'FR', 'say', 'set', 'H', 'run', '_el', 'setInterval', 'clearInterval',
  prelude + body + '\n return stepNetwork;',
)(shell, NET, PC, FR, say, set, H, run, _el, setIntervalStub, clearIntervalStub);

const main = async () => {
  await fn(false);
  if (!ap.onclick) throw new Error('no click handler was bound to the access point');
  const clicking = ap.onclick();                 // opens the prompt and waits, as a person does
  await new Promise(r => setImmediate(r));
  for (let i = 0; i < (plan.tickWhilePrompting || 0); i++) {
    if (tick) await tick();                      // the 2s watcher, while the dialog is open
  }
  log.runsDuringPrompt = log.runs;
  if (resolvePrompt) resolvePrompt('hunter2');   // the password is finally typed
  await clicking;
  process.stdout.write(JSON.stringify(log));
};
main().catch(e => { process.stdout.write(JSON.stringify({ error: String(e && e.message || e) })); });
