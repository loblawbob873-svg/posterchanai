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
/* DDC touches GPU I2C/AUX kernel paths. Some AMD display stacks fault inside dal_ddc_open and leave
 * IRQs disabled; polling it as routine tray status can therefore freeze the compositor. External
 * monitor brightness is opt-in until the administrator has verified DDC/CI on that hardware. */
const DDC_ENABLED = process.env.PC_ENABLE_DDC === '1';

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

/* External monitors do not appear under /sys/class/backlight. DDC/CI VCP code 0x10 is the
 * hardware brightness control and ddcutil is the small, standard userspace client for it. Kept
 * separate from brightness() so the synchronous sysfs tests and fast laptop path stay unchanged. */
async function ddcBrightness() {
  if(!DDC_ENABLED) return { available:false };
  try {
    const out = await run('ddcutil', ['getvcp', '10', '--brief'], 5000);
    const m = out.match(/VCP\s+10\s+[^\d]*([0-9]+)\s+([0-9]+)/i);
    if(!m || !(+m[2])) return { available:false };
    return { available:true, name:'DDC/CI monitor', ddc:true,
             percent:Math.round((+m[1]/+m[2])*100) };
  } catch (_) { return { available:false }; }
}

async function setBrightness(percent) {
  const p = panel();
  const want = Math.max(MIN_PERCENT, Math.min(100, Math.round(Number(percent) || 0)));
  if (!p) {
    if(!DDC_ENABLED) throw new Error('external-monitor brightness is disabled for safety; set PC_ENABLE_DDC=1 after verifying DDC/CI');
    try { await run('ddcutil', ['setvcp', '10', String(want)], 8000); return {percent:want,ddc:true}; }
    catch (_) { throw new Error('no controllable backlight — enable DDC/CI in the monitor menu'); }
  }
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

/* POWER PROFILES, FROM THE KERNEL — no daemon, no package.
 *
 * power-profiles-daemon is the usual answer and it is a wrapper: the firmware's own notion of a
 * profile is `/sys/firmware/acpi/platform_profile`, with its permitted values next to it in
 * `platform_profile_choices`. Reading those directly is fewer moving parts and one less thing to
 * install, which on a machine somebody else runs is worth more than the abstraction.
 *
 * NOTE: if power-profiles-daemon is ever installed it takes OWNERSHIP of that file, and writing to
 * it behind the daemon's back produces a profile the daemon does not know it is in. This profile
 * deliberately does not install it.
 *
 * The cpufreq governor is the fallback for hardware with no ACPI platform profile — a desktop, or
 * an older laptop. It is a coarser control and it is the one every machine has. */
const PLATFORM = path.join(SYS, 'firmware', 'acpi', 'platform_profile');
const CPUFREQ = path.join(SYS, 'devices', 'system', 'cpu', 'cpu0', 'cpufreq');

function profiles() {
  const choices = readStr(PLATFORM + '_choices');
  if (choices) {
    return { available: true, kind: 'platform', list: choices.split(/\s+/).filter(Boolean),
             active: readStr(PLATFORM) };
  }
  const avail = readStr(path.join(CPUFREQ, 'scaling_available_governors'));
  if (avail) {
    return { available: true, kind: 'governor', list: avail.split(/\s+/).filter(Boolean),
             active: readStr(path.join(CPUFREQ, 'scaling_governor')) };
  }
  return { available: false, kind: '', list: [], active: '' };
}

async function setProfile(name) {
  const n = String(name || '');
  const p = profiles();
  /* VALIDATED AGAINST WHAT THIS MACHINE ACTUALLY OFFERS, not against a pattern. The kernel rejects
   * an unknown value with EINVAL, which arrives here as an unhelpful write error; checking the list
   * first means the message names the profiles that exist. */
  if (!p.available) throw new Error('this machine has no power profiles');
  if (!p.list.includes(n)) throw new Error('no such profile — this machine has: ' + p.list.join(', '));
  const target = p.kind === 'platform' ? PLATFORM : path.join(CPUFREQ, 'scaling_governor');
  try {
    fs.writeFileSync(target, n);
  } catch (_) {
    throw new Error('cannot write ' + target + ' — the session needs a udev rule for it');
  }
  /* A GOVERNOR IS PER-CPU. Writing cpu0 changes cpu0, and a machine running one core at
   * `performance` and eleven at `powersave` is not in either profile. */
  if (p.kind === 'governor') {
    let cpus = [];
    try { cpus = fs.readdirSync(path.join(SYS, 'devices', 'system', 'cpu')); } catch (_) {}
    for (const c of cpus) {
      if (!/^cpu\d+$/.test(c)) continue;
      try { fs.writeFileSync(path.join(SYS, 'devices', 'system', 'cpu', c, 'cpufreq', 'scaling_governor'), n); }
      catch (_) {}
    }
  }
  return { active: n, kind: p.kind };
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
function hibernateConfigured() {
  return hibernateReady() && /resume=UUID=/.test(readStr('/etc/dracut.conf.d/90-posterchan-hibernate.conf'));
}
async function enableHibernation() {
  /* The first provisioned identity is the administrator and has a narrowly auditable sudo path.
   * -n is intentional: a GUI must never hang behind an invisible password prompt. */
  const out = await run('sudo', ['-n', '/usr/bin/gentoo.sh', 'hibernate'], 15 * 60 * 1000);
  return { ok:true, configured:hibernateConfigured(), rebootRequired:true, message:out.trim() };
}
const suspend = () => run('systemctl', ['suspend'], 20000).then(() => ({ ok: true }));
const hibernate = () => {
  if (!hibernateReady()) return Promise.reject(new Error('there is no swap to hibernate into'));
  return run('systemctl', ['hibernate'], 30000).then(() => ({ ok: true }));
};
const poweroff = () => run('systemctl', ['poweroff'], 20000).then(() => ({ ok: true }));
const reboot = () => run('systemctl', ['reboot'], 20000).then(() => ({ ok: true }));

const IDLE_HELPER = process.env.PC_IDLE_HELPER || '/usr/local/bin/pc-idle';
async function keepAwakeStatus() {
  try { return (await run(IDLE_HELPER, ['hold', 'status'], 3000)).trim() === 'on'; }
  catch (_) { return false; }
}
async function setKeepAwake(on) {
  await run(IDLE_HELPER, ['hold', on ? 'on' : 'off'], 5000);
  return { on: !!on };
}

async function idleTimeout() {
  const raw = (await run(IDLE_HELPER, ['get'], 3000)).trim();
  const seconds = Number(raw);
  if (!Number.isInteger(seconds) || seconds < 0) throw new Error('the display timeout is invalid');
  return seconds;
}

async function setIdleTimeout(seconds) {
  const n = Number(seconds);
  if (!Number.isInteger(n) || n < 0 || n > 86400)
    throw new Error('display timeout must be whole seconds from 0 to 86400');
  await run(IDLE_HELPER, ['set', String(n)], 5000);
  return { seconds: n };
}

/** Everything the shell needs to draw the panel, in one call. */
async function status() {
  let bright = brightness();
  if(!bright.available && DDC_ENABLED) bright = await ddcBrightness();
  return {
    brightness: bright,
    battery: battery(),
    profiles: await profiles(),
    keepAwake: await keepAwakeStatus(),
    idleSeconds: await idleTimeout().catch(() => 120),
    canHibernate: hibernateReady(),
    hibernateConfigured: hibernateConfigured(),
  };
}

module.exports = { brightness, ddcBrightness, setBrightness, battery, profiles, setProfile,
                   suspend, hibernate, poweroff, reboot, hibernateReady, hibernateConfigured,
                   enableHibernation, keepAwakeStatus,
                   setKeepAwake, idleTimeout, setIdleTimeout, status, MIN_PERCENT, DDC_ENABLED };
