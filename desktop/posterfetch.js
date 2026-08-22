/* Fast, dependency-free PosterChanOS terminal welcome.  This deliberately reads procfs/sysfs
 * instead of launching a chain of commands: every new tab should paint immediately, including
 * offline and half-provisioned live systems. */
'use strict';
const os = require('os');
const fs = require('fs');
const path = require('path');

const clean = (s, fallback) => String(s || fallback || '').replace(/[\r\n\x1b]/g, '').trim();
const one = (file) => { try { return fs.readFileSync(file, 'utf8').trim(); } catch (_) { return ''; } };
const human = n => {
  let x = Math.max(0, Number(n) || 0), i = 0;
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
  return `${x >= 10 || i === 0 ? x.toFixed(0) : x.toFixed(1)} ${units[i]}`;
};
const duration = seconds => {
  let n = Math.max(0, Math.floor(Number(seconds) || 0));
  const d = Math.floor(n / 86400); n %= 86400;
  const h = Math.floor(n / 3600); n %= 3600;
  const m = Math.floor(n / 60);
  return [d && `${d}d`, h && `${h}h`, (m || (!d && !h)) && `${m}m`].filter(Boolean).join(' ');
};

function osName() {
  const raw = one('/etc/os-release');
  const m = raw.match(/^PRETTY_NAME=(?:"([^"]*)"|'([^']*)'|([^\n]*))$/m);
  return clean(m && (m[1] || m[2] || m[3]), `${os.type()} ${os.release()}`);
}
function gpu() {
  try {
    for (const card of fs.readdirSync('/sys/class/drm').filter(x => /^card\d+$/.test(x))) {
      const dir = path.join('/sys/class/drm', card, 'device');
      const vendor = one(path.join(dir, 'vendor')).toLowerCase();
      const driver = one(path.join(dir, 'uevent')).match(/^DRIVER=(.+)$/m);
      const name = ({'0x1002':'AMD', '0x10de':'NVIDIA', '0x8086':'Intel'})[vendor] || '';
      if (name || driver) return clean([name, driver && driver[1]].filter(Boolean).join(' · '));
    }
  } catch (_) {}
  return 'not reported';
}
function network() {
  const rows = [];
  for (const [name, addrs] of Object.entries(os.networkInterfaces())) {
    const a = (addrs || []).find(x => x && x.family === 'IPv4' && !x.internal);
    if (a) rows.push(`${name} ${a.address}`);
  }
  return clean(rows.join(' · '), 'offline');
}
function storage(home) {
  try {
    const s = fs.statfsSync(home || os.homedir());
    return `${human((s.blocks - s.bfree) * s.bsize)} / ${human(s.blocks * s.bsize)}`;
  } catch (_) { return 'not reported'; }
}

function render(env) {
  const e = env || process.env;
  const cpus = os.cpus() || [];
  const cpu = clean(cpus[0] && cpus[0].model, 'unknown CPU').replace(/\s+/g, ' ');
  const used = os.totalmem() - os.freemem();
  const user = clean(e.USER, 'posterchan');
  const host = clean(os.hostname(), 'posterchanos');
  const C='\x1b[38;5;51m', M='\x1b[38;5;213m', D='\x1b[38;5;245m', W='\x1b[1;97m', R='\x1b[0m';
  const logo = [
    `${M}    ╱\/╲    ${R}`, `${M}   ╱${C}◢██◣${M}╲   ${R}`, `${M}  ◢${C}██████${M}◣  ${R}`,
    `${M}  ◥${C}██◤◥██${M}◤  ${R}`, `${M}   ╲${C}╲__╱${M}╱   ${R}`, `${M}    ╲__╱    ${R}`,
  ];
  const facts = [
    `${W}${user}@${host}${R}`,
    `${D}OS      ${R}${osName()}`,
    `${D}Kernel  ${R}${clean(os.release())} · ${clean(os.arch())}`,
    `${D}Uptime  ${R}${duration(os.uptime())}`,
    `${D}CPU     ${R}${cpu}${cpus.length ? ` · ${cpus.length} threads` : ''}`,
    `${D}RAM     ${R}${human(used)} / ${human(os.totalmem())}`,
    `${D}GPU     ${R}${gpu()}`,
    `${D}Disk    ${R}${storage(e.HOME)}`,
    `${D}Network ${R}${network()}`,
    `${D}Session ${R}${clean(e.XDG_SESSION_TYPE, 'tty')} · ${clean(e.SHELL, '/bin/bash')}`,
  ];
  const lines = [];
  for (let i=0; i<facts.length; i++) lines.push(`${logo[i] || '             '}  ${facts[i]}`);
  return `\r\n${lines.join('\r\n')}\r\n${C}  POSTERCHAN // OWN YOUR SIGNAL${R}\r\n\r\n`;
}

module.exports = { render, human, duration };
