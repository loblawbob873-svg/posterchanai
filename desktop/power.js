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

/* THE CHOSEN PROFILE HAS TO SURVIVE A REBOOT, AND SYSFS DOES NOT.
 *
 * A governor written to scaling_governor (and the ACPI platform_profile) is kernel runtime state:
 * it resets to the boot default on every reboot. So "power saver" — or "performance" — silently
 * reverted every time the machine came back, reported as "the powersave settings need to persist
 * after reboots, this is crazy". The idle timeout already persists this way (pc-idle writes a conf
 * and re-applies it at session start); the profile now does the same. The file is the single source
 * and restoreProfile() replays it from app.whenReady() in main.js. */
const PROFILE_CONF = process.env.PC_POWER_PROFILE_CONF
  || path.join(process.env.XDG_CONFIG_HOME || path.join(process.env.HOME || '/root', '.config'),
               'posterchanos', 'power-profile');

async function setProfile(name, opts) {
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
  // Persist the CHOICE (not on the restore replay, which is applying what is already saved).
  if (!opts || opts.persist !== false) {
    try { fs.mkdirSync(path.dirname(PROFILE_CONF), { recursive: true }); fs.writeFileSync(PROFILE_CONF, n + '\n'); }
    catch (_) {}
  }
  return { active: n, kind: p.kind };
}

/* Re-apply the saved profile at session start. A no-op when nothing was saved, when the saved value
 * is no longer offered (a kernel/hardware change), or when it is already active — so it is safe to
 * call unconditionally from main.js's app.whenReady(). Never throws: a machine that cannot set a
 * profile at boot must still reach the desktop. */
async function restoreProfile() {
  let want = '';
  try { want = fs.readFileSync(PROFILE_CONF, 'utf8').trim(); } catch (_) { return { restored: false, reason: 'none saved' }; }
  if (!want) return { restored: false, reason: 'none saved' };
  const p = profiles();
  if (!p.available || !p.list.includes(want)) return { restored: false, reason: 'not offered' };
  if (p.active === want) return { restored: false, reason: 'already active' };
  try { await setProfile(want, { persist: false }); return { restored: true, profile: want }; }
  catch (e) { return { restored: false, reason: String(e && e.message || e) }; }
}

/* SLEEP AND HIBERNATE go through systemd, which asks polkit, which normally allows a LOCAL ACTIVE
 * session to do it without a password. Hibernate additionally needs somewhere to write the image —
 * a machine with no swap cannot do it at all, and offering the button anyway is offering a button
 * that returns an error. */
const PROC_SWAPS = process.env.PC_PROC_SWAPS || '/proc/swaps';
const HIBERNATE_CONF = process.env.PC_HIBERNATE_CONF || '/etc/dracut.conf.d/90-posterchan-hibernate.conf';
function hibernateReady() {
  try {
    const sw = fs.readFileSync(PROC_SWAPS, 'utf8').trim().split('\n');
    return sw.length > 1;
  } catch (_) { return false; }
}
function hibernateConfigured() {
  return hibernateReady() && /resume=UUID=/.test(readStr(HIBERNATE_CONF));
}
async function enableHibernation() {
  /* The first provisioned identity is the administrator and has a narrowly auditable sudo path.
   * -n is intentional: a GUI must never hang behind an invisible password prompt. */
  const out = await run('sudo', ['-n', '/usr/bin/gentoo.sh', 'hibernate'], 15 * 60 * 1000);
  return { ok:true, configured:hibernateConfigured(), rebootRequired:true, message:out.trim() };
}

/* ── WHAT "SLEEP" MEANS ONCE HIBERNATION WORKS ──────────────────────────────────────────────────
 *
 * With hibernation enabled and ready, the default is SUSPEND-THEN-HIBERNATE: the machine sleeps in
 * RAM (instant wake) and, if nobody wakes it within the delay, writes itself to disk and powers off,
 * so a laptop left in a bag does not wake up flat. That is what the lid does (logind), what the
 * suspend key does, and what the desktop's own Sleep button does — the button used to run plain
 * `systemctl suspend`, which on a machine hibernation() had configured is not even available (the
 * old installer unlinked systemd-suspend.service), so "Sleep" failed on exactly the machines that
 * had asked for the better behaviour.
 *
 * The delay is systemd's `HibernateDelaySec`; "Never" means plain suspend. Both live in drop-ins
 * written by `gentoo.sh sleep-policy` (root), and are READ here from the same files systemd reads —
 * main file first, then `*.conf.d/*.conf` in order, last value wins — so what the panel shows is
 * what the lid will do, not what this process last asked for. */
const SYSTEMD_ETC = process.env.PC_SYSTEMD_ETC || '/etc/systemd';
const GENTOO_SH = process.env.PC_GENTOO_SH || '/usr/bin/gentoo.sh';
const SLEEP_DELAYS = [1800, 3600, 7200, 10800];      // 30 min, 1 h, 2 h, 3 h — plus 0 = Never
const DEFAULT_HIBERNATE_DELAY = 3600;

/* systemd time spans: "500", "90s", "30min", "1h", "2h 30min", "1h30m". Unknown → null. */
function parseSpan(v) {
  const s = String(v == null ? '' : v).trim().toLowerCase();
  if (!s) return null;
  if (/^\d+$/.test(s)) return Number(s);
  const unit = { us: 1e-6, ms: 1e-3, s: 1, sec: 1, second: 1, seconds: 1, m: 60, min: 60, minute: 60,
                 minutes: 60, h: 3600, hr: 3600, hour: 3600, hours: 3600, d: 86400, day: 86400, days: 86400 };
  const re = /(\d+(?:\.\d+)?)\s*([a-z]+)/g;
  // Every character must belong to a number+unit pair, or it is not a span this reads.
  if (s.replace(re, '').trim() !== '') return null;
  let total = 0, any = false, m;
  while ((m = re.exec(s))) {
    if (!(m[2] in unit)) return null;
    total += Number(m[1]) * unit[m[2]]; any = true;
  }
  return any ? Math.round(total) : null;
}

/* The value systemd will use for `[section] key`: the main file, then every drop-in in lexical
 * order, the last assignment winning. A drop-in directory that does not exist is simply none. */
function systemdValue(main, section, key) {
  const files = [path.join(SYSTEMD_ETC, main)];
  try {
    const dir = path.join(SYSTEMD_ETC, main + '.d');
    for (const f of fs.readdirSync(dir).filter((n) => n.endsWith('.conf')).sort()) files.push(path.join(dir, f));
  } catch (_) {}
  let val = null;
  for (const f of files) {
    let sec = '';
    for (const raw of readStr(f).split('\n')) {
      const line = raw.trim();
      if (!line || line[0] === '#' || line[0] === ';') continue;
      const h = line.match(/^\[(.+)\]$/);
      if (h) { sec = h[1]; continue; }
      if (sec !== section) continue;
      const eq = line.indexOf('=');
      if (eq > 0 && line.slice(0, eq).trim() === key) val = line.slice(eq + 1).trim();
    }
  }
  return val;
}

/** What the lid, the suspend key and the Sleep button do right now. */
function sleepPolicy() {
  const ready = hibernateConfigured();
  const lid = systemdValue('logind.conf', 'Login', 'HandleLidSwitch');
  /* Ready → suspend-then-hibernate unless somebody chose plain suspend ("Never"). Not ready → plain
   * suspend whatever the file says: suspend-then-hibernate on a machine that cannot hibernate is a
   * lid that does nothing. */
  const mode = ready && lid !== 'suspend' ? 'suspend-then-hibernate' : 'suspend';
  let delaySec = 0;
  if (mode === 'suspend-then-hibernate') {
    const d = parseSpan(systemdValue('sleep.conf', 'Sleep', 'HibernateDelaySec'));
    delaySec = d && d > 0 ? d : DEFAULT_HIBERNATE_DELAY;
  }
  return { hibernateReady: ready, mode, delaySec, choices: [0].concat(SLEEP_DELAYS),
           canChange: ready && gentooHasSleepPolicy() };
}

/* AN OLDER gentoo.sh DOES NOT KNOW THIS COMMAND, and its fallback for an unknown one is the
 * INTERACTIVE INSTALLER MENU — run under `sudo -n` with no terminal. So ask the file before asking
 * it to do anything: the desktop (asar) and the OS package (which ships gentoo.sh) update separately. */
function gentooHasSleepPolicy() {
  try { return /"\$1" = "sleep-policy"/.test(fs.readFileSync(GENTOO_SH, 'utf8')); }
  catch (_) { return false; }
}

async function setSleepPolicy(delaySec) {
  // A NUMBER, not something that coerces to one: Number(null) is 0, which is "never hibernate".
  const n = typeof delaySec === 'number' ? delaySec : NaN;
  if (!Number.isInteger(n) || !(n === 0 || SLEEP_DELAYS.includes(n)))
    throw new Error('choose one of the offered delays');
  if (n > 0 && !hibernateConfigured()) throw new Error('hibernation is not set up on this computer yet');
  if (!gentooHasSleepPolicy())
    throw new Error('this needs the latest PosterChanOS update — install it from Updates, then try again');
  await run(process.env.PC_SUDO || 'sudo', ['-n', GENTOO_SH, 'sleep-policy', String(n)], 60000);
  return sleepPolicy();
}

/* THE SLEEP BUTTON FOLLOWS THE POLICY. When suspend-then-hibernate is the policy and the kernel
 * refuses it anyway (a firmware that loses the resume image, a swap turned off since), plain
 * suspend is still better than a button that does nothing. */
async function suspend() {
  if (sleepPolicy().mode === 'suspend-then-hibernate') {
    try { await run('systemctl', ['suspend-then-hibernate'], 20000); return { ok: true, mode: 'suspend-then-hibernate' }; }
    catch (_) { /* fall through to plain suspend */ }
  }
  await run('systemctl', ['suspend'], 20000);
  return { ok: true, mode: 'suspend' };
}
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
    sleepPolicy: sleepPolicy(),
  };
}

module.exports = { brightness, ddcBrightness, setBrightness, battery, profiles, setProfile,
                   suspend, hibernate, poweroff, reboot, hibernateReady, hibernateConfigured,
                   enableHibernation, keepAwakeStatus, sleepPolicy, setSleepPolicy, parseSpan, systemdValue,
                   setKeepAwake, idleTimeout, setIdleTimeout, restoreProfile, status, MIN_PERCENT, DDC_ENABLED };
