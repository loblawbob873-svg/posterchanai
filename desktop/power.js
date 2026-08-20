/* Power, brightness, battery and sleep for the PosterChanOS shell.
 *
 * A desktop that cannot dim its screen or go to sleep is not a desktop, and every one of these is a
 * different mechanism with a different failure: brightness is a sysfs file, sleep is a systemd verb
 * behind polkit, profiles are a daemon that may not be installed, and battery is a directory that
 * does not exist on a tower. So each is asked for SEPARATELY and absent hardware is reported as
 * absent rather than as an error — a desktop machine has no battery and no backlight, and neither
 * of those is a fault to show somebody.
 *
 * BRIGHTNESS IS WRITTEN AS A PERCENTAGE, NEVER AS A RAW VALUE. `max_brightness` differs wildly
 * between panels — 255 on one, 96000 on another, 7 on some embedded ones — so a UI that stores raw
 * numbers gives a different screen on every machine, and a slider that "works" on the developer's
 * laptop is a black screen on somebody else's. And it CANNOT reach zero: on most panels 0 is off,
 * not dim, and a person who cannot see the screen cannot undo what they just did.
 *
 * The root path is overridable so the tests can drive the real code against a fake /sys instead of
 * the machine they run on.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');

const SYS = process.env.PC_SYSFS || '/sys';
const BACKLIGHT = path.join(SYS, 'class', 'backlight');
const POWER_SUPPLY = path.join(SYS, 'class', 'power_supply');
const MIN_PERCENT = 1;          // never off — see above

function run(bin, args, ms) {
  return new Promise((resolve, reject) => {
    execFile(bin, args, { timeout: ms || 15000 }, (err, stdout, stderr) => {
      if (err) return reject(new Error(String(stderr || err.message || err).trim().split('\n').pop()));
      resolve(String(stdout || ''));
    });
  });
}
const readNum = (p) => { try { return parseInt(fs.readFileSync(p, 'utf8').trim(), 10); } catch (_) { return null; } };
const readStr = (p) => { try { return fs.readFileSync(p, 'utf8').trim(); } catch (_) { return ''; } };

/* THE FIRST BACKLIGHT IS NOT ALWAYS THE RIGHT ONE. A laptop with a discrete GPU can expose several
 * — `intel_backlight`, `acpi_video0`, `nvidia_0` — and some of them are stubs that accept writes and
 * change nothing. The one with a sane max and a readable current value is the one that works. */
function panel() {
  let names = [];
  try { names = fs.readdirSync(BACKLIGHT); } catch (_) { return null; }
  const rated = names.map((n) => {
    const dir = path.join(BACKLIGHT, n);
    return { name: n, dir, max: readNum(path.join(dir, 'max_brightness')),
             cur: readNum(path.join(dir, 'brightness')) };
  }).filter((p) => p.max && p.max > 1 && p.cur !== null);
  if (!rated.length) return null;
  // Prefer a real panel driver over the ACPI shim, which is the one that is often a stub.
  rated.sort((a, b) => (a.name.startsWith('acpi') ? 1 : 0) - (b.name.startsWith('acpi') ? 1 : 0));
  return rated[0];
}

function brightness() {
  const p = panel();
  if (!p) return { available: false };
  return { available: true, name: p.name, percent: Math.round((p.cur / p.max) * 100) };
}

async function setBrightness(percent) {
  const p = panel();
  if (!p) throw new Error('this machine has no backlight');
  const want = Math.max(MIN_PERCENT, Math.min(100, Math.round(Number(percent) || 0)));
  const raw = Math.max(1, Math.round((want / 100) * p.max));
  const file = path.join(p.dir, 'brightness');
  try {
    fs.writeFileSync(file, String(raw));
  } catch (_) {
    /* SYSFS IS ROOT-OWNED UNLESS UDEV SAYS OTHERWISE. `brightnessctl` exists precisely to do this
     * without root and is worth trying before giving up — but if neither works the answer is a
     * missing udev rule, and saying THAT is the difference between a fixable machine and a slider
     * that does nothing. */
    try { await run('brightnessctl', ['set', want + '%']); }
    catch (_) {
      throw new Error('cannot write ' + file + ' — the session needs the video group or a udev rule');
    }
  }
  return { percent: want };
}

/** Battery, if there is one. A tower has none and that is not a fault. */
function battery() {
  let names = [];
  try { names = fs.readdirSync(POWER_SUPPLY); } catch (_) { return { present: false }; }
  const bats = names.filter((n) => /^BAT/i.test(n) || readStr(path.join(POWER_SUPPLY, n, 'type')) === 'Battery');
  if (!bats.length) return { present: false };
  const dir = path.join(POWER_SUPPLY, bats[0]);
  let pct = readNum(path.join(dir, 'capacity'));
  if (pct === null) {
    /* Some batteries report only charge_now/charge_full. A percentage computed from those is the
     * same number the kernel would have given; refusing to compute it shows "no battery" on a
     * laptop that plainly has one. */
    const now = readNum(path.join(dir, 'charge_now')) ?? readNum(path.join(dir, 'energy_now'));
    const full = readNum(path.join(dir, 'charge_full')) ?? readNum(path.join(dir, 'energy_full'));
    if (now !== null && full) pct = Math.round((now / full) * 100);
  }
  const status = readStr(path.join(dir, 'status')) || 'Unknown';
  return { present: true, name: bats[0], percent: pct, status,
           charging: /charging|full/i.test(status) && !/discharging/i.test(status) };
}

/* POWER PROFILES. power-profiles-daemon is the standard interface and may not be installed; cpupower
 * governors are the fallback and need root. Absent both, the feature is absent — reported, not
 * thrown, so the UI can leave the control out instead of showing one that always fails. */
async function profiles() {
  try {
    const out = await run('powerprofilesctl', ['list']);
    const list = [...String(out).matchAll(/^\s*\*?\s*([a-z-]+):/gm)].map((m) => m[1]);
    let active = '';
    try { active = (await run('powerprofilesctl', ['get'])).trim(); } catch (_) {}
    if (list.length) return { available: true, list, active };
  } catch (_) {}
  return { available: false, list: [], active: '' };
}
async function setProfile(name) {
  const n = String(name || '');
  if (!/^[a-z-]+$/.test(n)) throw new Error('not a profile name');
  await run('powerprofilesctl', ['set', n]);
  return { active: n };
}

/* SLEEP AND HIBERNATE go through systemd, which asks polkit, which normally allows a LOCAL ACTIVE
 * session to do it without a password. Hibernate additionally needs somewhere to write the image —
 * a machine with no swap cannot do it at all, and offering the button anyway is offering a button
 * that returns an error. */
function hibernateReady() {
  try {
    const sw = fs.readFileSync('/proc/swaps', 'utf8').trim().split('\n');
    return sw.length > 1;
  } catch (_) { return false; }
}
const suspend = () => run('systemctl', ['suspend'], 20000).then(() => ({ ok: true }));
const hibernate = () => {
  if (!hibernateReady()) return Promise.reject(new Error('there is no swap to hibernate into'));
  return run('systemctl', ['hibernate'], 30000).then(() => ({ ok: true }));
};
const poweroff = () => run('systemctl', ['poweroff'], 20000).then(() => ({ ok: true }));
const reboot = () => run('systemctl', ['reboot'], 20000).then(() => ({ ok: true }));

/** Everything the shell needs to draw the panel, in one call. */
async function status() {
  return {
    brightness: brightness(),
    battery: battery(),
    profiles: await profiles(),
    canHibernate: hibernateReady(),
  };
}

module.exports = { brightness, setBrightness, battery, profiles, setProfile,
                   suspend, hibernate, poweroff, reboot, hibernateReady, status, MIN_PERCENT };
