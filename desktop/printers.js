/* Printers for the PosterChanOS shell — because CUPS's own admin pages can never be logged into.
 *
 * CUPS is installed and running on every PosterChanOS machine, and its web UI is the only way to
 * add a printer. It authenticates a SYSTEM account through PAM, and `pc-provision-user` says why
 * that can never work here:
 *
 *   "Identity accounts deliberately have no Unix password: authentication happened at the Nostr
 *    sign-in screen."
 *
 * So the one account on the machine has no password to type, and 127.0.0.1:631/admin refuses it
 * for ever. Adding the user to `lp`/`lpadmin` was necessary and not sufficient. Reported as "how
 * are users supposed to install a printer" and "i can't login cups web".
 *
 * The answer is not to invent a password. That account already holds `NOPASSWD: ALL` sudo — the
 * grant the first owner is provisioned with — so the shell can run the CUPS command-line tools
 * directly, exactly as Displays and Power already drive their hardware. No password, no web UI, no
 * second identity system.
 *
 * WHAT IS DELIBERATELY NOT HERE: nothing takes a raw command. Every call below is a fixed argv with
 * the user's values passed as separate arguments, never interpolated into a shell string — a
 * printer NAME and a device URI both come from the network, and `lpadmin -p "$name"` through a
 * shell is a remote device advertising itself into a command line.
 */
'use strict';
const { execFile } = require('child_process');

const SUDO = process.env.PC_SUDO || 'sudo';
/* CUPS names allow letters, digits, dash and underscore — no spaces, slashes or '#'. Enforced here
 * rather than trusted from the page, because this module is the privileged side. */
const NAME_OK = /^[A-Za-z0-9_-]{1,127}$/;

function run(bin, args, ms) {
  return new Promise((resolve) => {
    execFile(bin, args, { timeout: ms || 20000 }, (err, stdout, stderr) => {
      resolve({ ok: !err, out: String(stdout || ''), err: String((stderr || '') || (err && err.message) || '') });
    });
  });
}

/* `lpstat -v` prints "device for NAME: URI". Parsed rather than `-l`, which changes shape between
 * CUPS versions and carries pages of options nobody asked for. */
function parseDevices(out) {
  const list = [];
  for (const line of out.split('\n')) {
    const m = /^device for ([^:]+):\s*(.+)$/.exec(line.trim());
    if (m) list.push({ name: m[1], uri: m[2] });
  }
  return list;
}

/* `lpstat -p` prints "printer NAME is idle. enabled since ..." — the state, which is the half a
 * person actually looks at when something has not come out. */
function parseState(out) {
  const state = {};
  for (const line of out.split('\n')) {
    const m = /^printer (\S+) is (\S+?)\.?\s/.exec(line.trim());
    if (m) state[m[1]] = m[2];
  }
  return state;
}

async function status() {
  const [devices, states, def] = await Promise.all([
    run('lpstat', ['-v']), run('lpstat', ['-p']), run('lpstat', ['-d']),
  ]);
  /* CUPS not running is not "no printers": it is a machine that cannot print at all, and saying so
   * is the difference between a person adding a printer and a person wondering why nothing works. */
  if (!devices.ok && /cups-?d|Connection refused|No such file/i.test(devices.err)) {
    return { available: false, reason: 'the printing service (cups) is not running', printers: [] };
  }
  const state = parseState(states.out);
  const defaultName = (/system default destination:\s*(\S+)/i.exec(def.out) || [])[1] || '';
  return {
    available: true,
    defaultName,
    printers: parseDevices(devices.out).map(p => ({ ...p, state: state[p.name] || 'unknown' })),
  };
}

/* Everything CUPS can see right now: USB, and whatever answered DNS-SD on the network. The `-l`
 * form is avoided for the reason above; `lpinfo -v` lines are "class uri" or "network uri". */
async function discover() {
  const r = await run('lpinfo', ['-v'], 25000);
  const found = [];
  for (const line of r.out.split('\n')) {
    const m = /^(\S+)\s+(\S.*)$/.exec(line.trim());
    if (!m) continue;
    const [, kind, uri] = m;
    /* The bare scheme lines ("network beh", "network http") are CUPS listing its own BACKENDS, not
       devices. A person offered "https" as a printer would rightly think this was broken. */
    if (!uri.includes('://')) continue;
    found.push({ kind, uri });
  }
  return { ok: r.ok, devices: found, error: r.ok ? '' : r.err.slice(0, 400) };
}

/* THE DRIVER DEPENDS ON THE URI, and hardcoding one is how this failed on the first real printer it
 * ever met. `-m everywhere` is IPP Everywhere — the driverless path a modern network printer wants,
 * and the right default — but lpadmin refuses it outright for anything that is not an IPP
 * connection: "IPP Everywhere driver requires an IPP connection". Measured against a Brother on a
 * real network, which CUPS discovers as `lpd://brw…/BINARY_P1`: every add failed, and the panel
 * would have looked broken for exactly the printers people already own.
 *
 * So the model is chosen by scheme, and a refusal still falls back to a queue with no model at all
 * — CUPS then treats it as raw, which is what an LPD or socket printer expects. Trying rather than
 * assuming, because `lpinfo` reports the transport, never whether the far end speaks IPP. */
const IPP_URI = /^(?:ipp|ipps|dnssd|mdns)s?:/i;

async function add({ name, uri, description }) {
  const n = String(name || '').trim();
  if (!NAME_OK.test(n)) return { ok: false, error: 'a printer name may use letters, digits, - and _' };
  const u = String(uri || '').trim();
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(u)) return { ok: false, error: 'that is not a device URI' };
  const desc = String(description || '').trim();
  const attempt = async (model) => {
    const args = [SUDO === 'sudo' ? '-n' : '', 'lpadmin', '-p', n, '-v', u].filter(Boolean);
    if (model) args.push('-m', model);
    args.push('-E');
    if (desc) args.push('-D', desc.slice(0, 120));
    return run(SUDO, args, 40000);
  };
  let r = await attempt(IPP_URI.test(u) ? 'everywhere' : '');
  /* The one retry worth making: the driverless model was refused for this transport. Anything else
     — an unreachable host, a name already taken, no permission — is reported as CUPS said it. */
  if (!r.ok && /requires an IPP connection|no such (?:file|driver)/i.test(r.err)) r = await attempt('');
  return { ok: r.ok, error: r.ok ? '' : (r.err || 'lpadmin refused that printer').slice(0, 400) };
}

async function setDefault(name) {
  const n = String(name || '').trim();
  if (!NAME_OK.test(n)) return { ok: false, error: 'unknown printer' };
  const r = await run(SUDO, [SUDO === 'sudo' ? '-n' : '', 'lpadmin', '-d', n].filter(Boolean));
  return { ok: r.ok, error: r.ok ? '' : r.err.slice(0, 400) };
}

async function remove(name) {
  const n = String(name || '').trim();
  if (!NAME_OK.test(n)) return { ok: false, error: 'unknown printer' };
  const r = await run(SUDO, [SUDO === 'sudo' ? '-n' : '', 'lpadmin', '-x', n].filter(Boolean));
  return { ok: r.ok, error: r.ok ? '' : r.err.slice(0, 400) };
}

/* A TEST PAGE, because "it is in the list" is not "it prints". Adding a queue succeeds against a
 * URI that no longer answers, and the only honest proof is paper. */
async function testPage(name) {
  const n = String(name || '').trim();
  if (!NAME_OK.test(n)) return { ok: false, error: 'unknown printer' };
  const r = await run('lp', ['-d', n, '/usr/share/cups/data/testprint'], 30000);
  return { ok: r.ok, out: r.out.trim().slice(0, 200), error: r.ok ? '' : r.err.slice(0, 400) };
}

module.exports = { status, discover, add, setDefault, remove, testPage,
                   _parseDevices: parseDevices, _parseState: parseState };
