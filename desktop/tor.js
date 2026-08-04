/* Native Tor for the desktop app.
 *
 * The app SHIPS tor (the Tor Project's "expert bundle", fetched per platform by
 * .github/workflows/desktop.yml and packed as an extraResource), starts it as a child process, and
 * points the whole Electron session at its SOCKS port. Nothing here talks to a system tor: a bundled
 * binary is the only way "turn on Tor" can mean the same thing on a clean Windows box as on a Linux
 * machine that already runs one, and the whole feature is worthless if it silently isn't there.
 *
 * Four things in here are load-bearing and each fails SILENTLY if you drop it:
 *
 *  1. GeoIPFile / GeoIPv6File. `ExitNodes {us}` needs tor's geoip database to know which relay is
 *     where. Without those lines tor starts, bootstraps, reports 100% and routes through whatever
 *     country it likes — the exit-country setting becomes decoration, and nothing anywhere says so.
 *     The expert bundle ships them under data/; that is the only reason data/ is packaged at all.
 *  2. StrictNodes 1, but ONLY alongside ExitNodes. It is what makes a country a guarantee: tor
 *     refuses to build a circuit rather than quietly exiting somewhere else. On its own (no
 *     ExitNodes) it is meaningless, and with a country the user did not ask for it would be a
 *     footgun, which is why it is written conditionally.
 *  3. The control port + its cookie. "New circuit" is a NEWNYM signal and there is no other way to
 *     ask for one; bootstrap progress we read from stdout, which needs no auth.
 *  4. Ephemeral ports. A fixed 9050 collides with a system tor, and a system tor is exactly what a
 *     Tor user has. The collision shows up as "tor exited immediately", i.e. as a broken app.
 */
const { app } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const net = require('net');

// Exit countries offered in the UI. A curated list, not tor's full geoip set: the point of the picker
// is "somewhere plausible", and a 250-entry dropdown mostly full of countries with no exit relays at
// all would be a worse answer than a short one. Any two-letter code still works if typed into torrc by
// hand; this is the menu, not the limit.
const COUNTRIES = [
  ['us', 'United States'], ['ca', 'Canada'], ['gb', 'United Kingdom'], ['ie', 'Ireland'],
  ['nl', 'Netherlands'], ['de', 'Germany'], ['fr', 'France'], ['ch', 'Switzerland'],
  ['at', 'Austria'], ['se', 'Sweden'], ['no', 'Norway'], ['fi', 'Finland'], ['dk', 'Denmark'],
  ['is', 'Iceland'], ['es', 'Spain'], ['pt', 'Portugal'], ['it', 'Italy'], ['pl', 'Poland'],
  ['cz', 'Czechia'], ['ro', 'Romania'], ['bg', 'Bulgaria'], ['ua', 'Ukraine'], ['ee', 'Estonia'],
  ['lv', 'Latvia'], ['lt', 'Lithuania'], ['lu', 'Luxembourg'], ['md', 'Moldova'],
  ['jp', 'Japan'], ['sg', 'Singapore'], ['hk', 'Hong Kong'], ['au', 'Australia'],
  ['nz', 'New Zealand'], ['br', 'Brazil'], ['ar', 'Argentina'], ['za', 'South Africa'],
  ['il', 'Israel'], ['in', 'India'], ['kr', 'South Korea'], ['th', 'Thailand'],
];
const COUNTRY_NAMES = new Map(COUNTRIES);

// ---- where the bundled binary lives -------------------------------------------------------------
// Packed layout mirrors the expert bundle's own, unchanged, so a version bump is a re-extract:
//   <resources>/tor/tor/tor(.exe)      the binary + its shared libraries
//   <resources>/tor/data/geoip{,6}     the country database (see note 1 above)
function bundleRoot() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'tor')
    : path.join(__dirname, 'resources', 'tor');
}
function torBinary() {
  const exe = process.platform === 'win32' ? 'tor.exe' : 'tor';
  // Both shapes are accepted because the archives have not always agreed on whether the binary sits
  // in tor/ or at the root, and a layout change must not silently disable the feature.
  for (const p of [path.join(bundleRoot(), 'tor', exe), path.join(bundleRoot(), exe)]) {
    try { if (fs.statSync(p).isFile()) return p; } catch (_) {}
  }
  return null;
}
function geoipFiles() {
  const out = {};
  for (const [key, name] of [['geoip', 'geoip'], ['geoip6', 'geoip6']]) {
    for (const p of [path.join(bundleRoot(), 'data', name), path.join(bundleRoot(), name)]) {
      try { if (fs.statSync(p).isFile()) { out[key] = p; break; } } catch (_) {}
    }
  }
  return out;
}

// A free localhost port, asked of the OS rather than guessed — see note 4.
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
  });
}

const state = {
  enabled: false,
  country: '',          // '' = any
  proc: null,
  socksPort: 0,
  controlPort: 0,
  cookiePath: '',
  progress: null,       // 0..100 while bootstrapping
  bootstrapped: false,
  error: '',
  starting: false,
};

let onChange = () => {};
function setOnChange(fn) { onChange = typeof fn === 'function' ? fn : () => {}; }
function emit() { try { onChange(status()); } catch (_) {} }

function status() {
  return {
    enabled: state.enabled,
    running: !!state.proc,
    bootstrapped: state.bootstrapped,
    progress: state.progress,
    country: state.country,
    countryName: COUNTRY_NAMES.get(state.country) || '',
    socksPort: state.socksPort,
    error: state.error,
    available: !!torBinary(),
    countries: COUNTRIES,
  };
}

function dataDir() {
  const d = path.join(app.getPath('userData'), 'tor');
  fs.mkdirSync(d, { recursive: true });
  // tor REFUSES to start if its DataDirectory is group/world readable ("Permissions on directory
  // ... are too permissive"). Electron's userData is 0700 on macOS but not reliably on Linux.
  if (process.platform !== 'win32') { try { fs.chmodSync(d, 0o700); } catch (_) {} }
  return d;
}

function writeTorrc() {
  const d = dataDir();
  const geo = geoipFiles();
  const lines = [
    '# Written by the PosterChan desktop app. Edits are overwritten on every start.',
    `SocksPort 127.0.0.1:${state.socksPort}`,
    `ControlPort 127.0.0.1:${state.controlPort}`,
    'CookieAuthentication 1',
    `DataDirectory ${d}`,
    'ClientOnly 1',
    'AvoidDiskWrites 1',
    // Bootstrap progress is parsed off stdout, so the level has to be notice and the sink stdout.
    'Log notice stdout',
  ];
  if (geo.geoip) lines.push(`GeoIPFile ${geo.geoip}`);
  if (geo.geoip6) lines.push(`GeoIPv6File ${geo.geoip6}`);
  if (state.country) {
    // StrictNodes belongs with ExitNodes and nowhere else — see note 2.
    lines.push(`ExitNodes {${state.country}}`);
    lines.push('StrictNodes 1');
  }
  const p = path.join(d, 'torrc');
  fs.writeFileSync(p, lines.join('\n') + '\n');
  return p;
}

function stop() {
  const p = state.proc;
  state.proc = null;
  state.progress = null;
  state.bootstrapped = false;
  if (p) { try { p.kill(); } catch (_) {} }
}

async function start() {
  const bin = torBinary();
  if (!bin) {
    state.error = 'This build does not include Tor.';
    emit();
    return false;
  }
  stop();
  state.error = '';
  state.starting = true;
  try {
    state.socksPort = await freePort();
    state.controlPort = await freePort();
  } catch (e) {
    state.starting = false;
    state.error = 'Could not reserve a local port for Tor.';
    emit();
    return false;
  }
  const torrc = writeTorrc();
  state.cookiePath = path.join(dataDir(), 'control_auth_cookie');
  let proc;
  try {
    proc = spawn(bin, ['-f', torrc], {
      // cwd at the bundle root so tor finds its co-located shared libraries on Linux/macOS.
      cwd: path.dirname(bin),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch (e) {
    state.starting = false;
    state.error = 'Tor failed to start: ' + ((e && e.message) || e);
    emit();
    return false;
  }
  state.proc = proc;
  state.starting = false;

  /* Line-buffered, and it has to be BOTH ways round:
   *
   * One chunk can carry SEVERAL lines — on a fast connection tor emits 10%, 45% and 100% close enough
   * together to land in one read. A single .exec() takes the FIRST match, so progress stuck at 10 and
   * `bootstrapped` never became true: waitBootstrapped() then sat on the boot card until its two-minute
   * timeout and declared a working Tor broken, worst on the fastest links.
   *
   * And one line can be SPLIT across two chunks, so a `Bootstrapped 100%` cut mid-number would be
   * missed by both halves. Hence the carry buffer rather than per-chunk matching.
   */
  let carry = '';
  const readLines = (chunk) => {
    carry += String(chunk);
    const lines = carry.split(/\r?\n/);
    carry = lines.pop();                       // the last piece may be a partial line — keep it
    // Cap the carry so a tor that somehow emits no newline can't grow it without bound.
    if (carry.length > 8192) carry = carry.slice(-1024);
    let moved = false;
    for (const line of lines) {
      const m = /Bootstrapped (\d+)/.exec(line);
      if (m) {
        const pct = Math.min(100, parseInt(m[1], 10));
        // Monotonic: tor can re-report a lower number while rebuilding, and a progress bar that goes
        // backwards after reaching 100 would un-launch the app.
        if (state.progress == null || pct > state.progress) { state.progress = pct; moved = true; }
        if (pct >= 100) { state.bootstrapped = true; moved = true; }
      }
      // A country with no usable exits is the one failure the user CAUSED and can fix, so name it
      // instead of leaving them on a number that never moves. StrictNodes makes this fatal by design.
      if (state.country && /no known exit|All routers are down|Failed to find node for hop/i.test(line)) {
        state.error = 'No Tor exit available in '
          + (COUNTRY_NAMES.get(state.country) || state.country.toUpperCase())
          + '. Pick another country or choose Any.';
        moved = true;
      }
    }
    if (moved) emit();
  };
  if (proc.stdout) proc.stdout.on('data', readLines);
  if (proc.stderr) proc.stderr.on('data', readLines);
  proc.on('exit', (code) => {
    if (state.proc !== proc) return;      // superseded by a restart — not this process's news
    state.proc = null;
    state.bootstrapped = false;
    state.progress = null;
    // Deliberately NOT "so we turned Tor off". The proxy stays pointed at a dead SOCKS port, which
    // fails every request — that is the fail-CLOSED half of the promise the switch makes. Clearing it
    // here would silently drop the user onto the clear net at the worst possible moment.
    if (state.enabled && !state.error) {
      state.error = 'Tor stopped unexpectedly' + (code == null ? '' : ' (exit ' + code + ')')
        + '. Traffic is blocked until it restarts.';
    }
    emit();
  });
  emit();
  return true;
}

// SIGNAL NEWNYM over the control port: authenticate with the cookie tor wrote, ask for fresh
// circuits, hang up. Resolves false on any failure rather than throwing — a failed "new circuit" is
// an inconvenience, not an error worth a dialog.
function newCircuit() {
  return new Promise((resolve) => {
    if (!state.proc || !state.controlPort) return resolve(false);
    let cookie;
    try { cookie = fs.readFileSync(state.cookiePath); } catch (_) { return resolve(false); }
    const sock = net.connect(state.controlPort, '127.0.0.1');
    let stage = 0, done = false;
    const finish = (ok) => { if (done) return; done = true; try { sock.end(); } catch (_) {} resolve(ok); };
    sock.setTimeout(5000, () => finish(false));
    sock.on('error', () => finish(false));
    sock.on('connect', () => sock.write('AUTHENTICATE ' + cookie.toString('hex') + '\r\n'));
    sock.on('data', (d) => {
      if (!String(d).startsWith('250')) return finish(false);
      if (stage === 0) { stage = 1; sock.write('SIGNAL NEWNYM\r\n'); return; }
      finish(true);
    });
  });
}

/* Turn Tor on/off, or change the exit country. A country change RESTARTS tor: ExitNodes can be set
 * over the control port, but StrictNodes cannot be changed on a running instance, and applying half
 * the pair would leave the country as a preference rather than the guarantee the UI promises. */
async function set(opts) {
  opts = opts || {};
  let restart = false;
  if (typeof opts.country === 'string') {
    const cc = opts.country.trim().toLowerCase();
    if (/^[a-z]{2}$/.test(cc) || cc === '') {
      if (cc !== state.country) { state.country = cc; restart = state.enabled; }
    }
  }
  if (typeof opts.enabled === 'boolean' && opts.enabled !== state.enabled) {
    state.enabled = opts.enabled;
    if (!state.enabled) { stop(); state.error = ''; emit(); return status(); }
    restart = true;
  }
  if (restart && state.enabled) { state.error = ''; await start(); }
  return status();
}

// Restore the saved choice at launch. Returns whether tor is wanted, so the caller knows to hold the
// window rather than having to reach into the state itself.
async function init(saved) {
  saved = saved || {};
  state.country = /^[a-z]{2}$/.test(String(saved.country || '').toLowerCase())
    ? String(saved.country).toLowerCase() : '';
  state.enabled = !!saved.enabled;
  if (state.enabled) await start();
  return state.enabled;
}

// Wait for bootstrap (or a hard error). Resolves true only when tor is actually usable, so the caller
// can hold the window on a progress card and know when it is safe to load anything.
function waitBootstrapped(timeoutMs) {
  return new Promise((resolve) => {
    if (!state.enabled) return resolve(true);
    if (state.bootstrapped) return resolve(true);
    const started = Date.now();
    const tick = setInterval(() => {
      if (state.bootstrapped) { clearInterval(tick); return resolve(true); }
      if (!state.enabled) { clearInterval(tick); return resolve(true); }
      // A dead process or a country with no exits will never bootstrap; stop waiting on it.
      if (state.error && !state.proc) { clearInterval(tick); return resolve(false); }
      if (Date.now() - started > (timeoutMs || 120000)) {
        clearInterval(tick);
        if (!state.error) state.error = 'Tor did not connect within two minutes.';
        return resolve(false);
      }
    }, 250);
  });
}

// The proxy rules for a session. Exported rather than applied here so the one place that owns the
// session owns the switch too.
function proxyRules() { return 'socks5://127.0.0.1:' + state.socksPort; }

module.exports = { init, set, status, start, stop, newCircuit, waitBootstrapped, proxyRules,
                   setOnChange, COUNTRIES, available: () => !!torBinary() };
