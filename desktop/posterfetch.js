/* Fast, dependency-free PosterChanOS terminal welcome.  This deliberately reads procfs/sysfs
 * instead of launching a chain of commands: every new tab should paint immediately, including
 * offline and half-provisioned live systems.
 *
 * IT IS THE FIRST THING THE MACHINE SAYS, so it is allowed to look like something — but never at
 * the cost of the two properties above. Everything drawn here is computed from values already in
 * hand: the gradient is arithmetic, the meters are two numbers each, and the colour strip is a
 * string. No subprocess, no probe, no await.
 */
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

const R = '\x1b[0m';
const BOLD = '\x1b[1m';

/* TRUECOLOR WHERE THERE IS TRUECOLOR, and a 256-colour ramp where there is not.
 *
 * The gradient is the whole look, and 256-colour mode has no smooth magenta→cyan ramp — picking the
 * nearest cube entry per row gives visible banding, which is worse than not trying. So the fallback
 * is a hand-picked run of cube colours that steps cleanly instead of a conversion that stripes. */
const truecolor = (env) => /truecolor|24bit/i.test(String((env && env.COLORTERM) || ''));
const RAMP_256 = [201, 200, 199, 198, 205, 171, 141, 111, 81, 51, 45, 39];
function ramp(env, i, n) {
  const t = n <= 1 ? 0 : Math.max(0, Math.min(1, i / (n - 1)));
  if (!truecolor(env)) return `\x1b[38;5;${RAMP_256[Math.round(t * (RAMP_256.length - 1))]}m`;
  // #ff4fd8 → #00f0ff, the client's own two accents.
  const r = Math.round(255 + t * (0 - 255));
  const g = Math.round(79 + t * (240 - 79));
  const b = Math.round(216 + t * (255 - 216));
  return `\x1b[38;2;${r};${g};${b}m`;
}
const rgb = (env, r, g, b, n256) => truecolor(env) ? `\x1b[38;2;${r};${g};${b}m` : `\x1b[38;5;${n256}m`;

function osName() {
  const raw = one('/etc/os-release');
  const m = raw.match(/^PRETTY_NAME=(?:"([^"]*)"|'([^']*)'|([^\n]*))$/m);
  return clean(m && (m[1] || m[2] || m[3]), `${os.type()} ${os.release()}`);
}
let _pciIds = null;
function pciIdsText() {
  if (_pciIds !== null) return _pciIds;
  _pciIds = '';
  for (const file of ['/usr/share/hwdata/pci.ids', '/usr/share/misc/pci.ids', '/usr/share/pci.ids']) {
    const text = one(file);
    if (text) { _pciIds = text; break; }
  }
  return _pciIds;
}
/* Resolve one PCI_ID without executing lspci. pci.ids is already shipped for hardware discovery,
 * and reading it once per Electron process keeps every later terminal tab dependency-free and
 * instant. Vendor records start at column zero; device records are one tab beneath their vendor. */
function pciModel(ids, vendor, device) {
  const wantV = String(vendor || '').replace(/^0x/i, '').toLowerCase();
  const wantD = String(device || '').replace(/^0x/i, '').toLowerCase();
  if (!ids || !wantV || !wantD) return '';
  let inVendor = false;
  for (const line of String(ids).split(/\r?\n/)) {
    const vm = line.match(/^([0-9a-fA-F]{4})\s+(.+)$/);
    if (vm) { inVendor = vm[1].toLowerCase() === wantV; continue; }
    if (!inVendor) continue;
    const dm = line.match(/^\t([0-9a-fA-F]{4})\s+(.+)$/);
    if (dm && dm[1].toLowerCase() === wantD) return clean(dm[2]);
  }
  return '';
}
function gpu() {
  try {
    const rows = [];
    for (const card of fs.readdirSync('/sys/class/drm').filter(x => /^card\d+$/.test(x))) {
      const dir = path.join('/sys/class/drm', card, 'device');
      const vendor = one(path.join(dir, 'vendor')).toLowerCase();
      const uevent = one(path.join(dir, 'uevent'));
      const driver = uevent.match(/^DRIVER=(.+)$/m);
      const pci = uevent.match(/^PCI_ID=([0-9a-f]{4}):([0-9a-f]{4})$/im);
      const maker = ({'0x1002':'AMD', '0x10de':'NVIDIA', '0x8086':'Intel'})[vendor] || '';
      const model = pci ? pciModel(pciIdsText(), pci[1], pci[2]) : '';
      const label = clean([maker, model || (driver && driver[1])].filter(Boolean).join(' '));
      if (label && !rows.includes(label)) rows.push(label);
    }
    if (rows.length) return rows.join(' / ');
  } catch (_) {}
  return 'not reported';
}
function network() {
  const rows = [];
  /* This is decoration, never a dependency of the local shell. libuv can reject interface
   * enumeration while networking is being reconfigured; letting that exception escape aborts
   * localterm.start before the prompt exists. */
  try {
    for (const [name, addrs] of Object.entries(os.networkInterfaces() || {})) {
      const a = (addrs || []).find(x => x && x.family === 'IPv4' && !x.internal);
      if (a) rows.push(`${name} ${a.address}`);
    }
  } catch (_) { return 'offline'; }
  return clean(rows.join(' · '), 'offline');
}
function diskBytes(home) {
  try {
    const s = fs.statfsSync(home || os.homedir());
    return { used: (s.blocks - s.bfree) * s.bsize, total: s.blocks * s.bsize };
  } catch (_) { return null; }
}

/* A METER, because "33 GiB / 63 GiB" is a division the reader should not have to do. Coloured by
 * how full it is and not by the palette: this is the one place on screen carrying a warning, and a
 * disk at 96% should not be the same colour as one at 4% merely because the theme is cyan. */
function meter(env, used, total, width) {
  const w = Math.max(6, width || 12);
  const frac = total > 0 ? Math.max(0, Math.min(1, used / total)) : 0;
  const on = Math.round(frac * w);
  const col = frac >= 0.9 ? rgb(env, 255, 92, 110, 203)
            : frac >= 0.75 ? rgb(env, 255, 196, 92, 214)
            : rgb(env, 0, 240, 255, 51);
  const dim = rgb(env, 78, 70, 108, 238);
  return `${col}${'▰'.repeat(on)}${dim}${'▱'.repeat(w - on)}${R} `
       + `${col}${String(Math.round(frac * 100)).padStart(3)}%${R}`;
}

/* A terminal portrait of the PosterChan mascot: cat ears, long hair, closed-eye smile and hoodie.
 * It deliberately uses plain ASCII rather than trying to print the PNG itself — local shells also
 * open over SSH/serial where sixel/kitty graphics are unavailable, while these cells remain the
 * same likeness and width everywhere. Eleven rows, nineteen columns, every row the same display
 * width so the fact column cannot rag itself when a row happens to be shorter. */
const LOGO = [
  '      /\\   /\\      ',
  '     /  \\_/  \\     ',
  '    / /~~~~~\\ \\    ',
  '   | | ^   ^ | |   ',
  '   | |   ^   | |   ',
  '   |  \\ \\_/ /  |   ',
  '    \\  `---\'  /    ',
  '     `-. | .-\'     ',
  '       /|||\\       ',
  '      / ||| \\      ',
  '     /__|||__\\     ',
];
const LOGO_W = 19;

function render(env, cols) {
  const e = env || process.env;
  const width = Math.max(20, Math.min(400, Number(cols) || 100));
  /* Declared HERE, not beside the loop that uses it: the rule and the meters below both size
   * themselves against the logo column, and reading it later was a temporal-dead-zone throw —
   * which, in a banner rendered on the way to a prompt, is a tab that opens to a stack trace. */
  const wide = width >= LOGO_W + 46;   // room for the logo column AND a readable fact column
  const cpus = os.cpus() || [];
  /* The model string as the kernel reports it is marketing, not information: "AMD Ryzen 5 5600X
   * 6-Core Processor" and "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz". The core count is printed
   * beside it already, so the parts that are dropped are the ones nobody reads. */
  const cpu = clean(cpus[0] && cpus[0].model, 'unknown CPU').replace(/\s+/g, ' ')
                .replace(/\((R|TM|r|tm)\)/g, '')
                .replace(/\s*\d+-Core\s+Processor/i, '')
                .replace(/\s+Processor$/i, '')
                .replace(/\s+CPU\s+/i, ' ')
                .replace(/\s+/g, ' ').trim();
  const usedMem = os.totalmem() - os.freemem();
  const disk = diskBytes(e.HOME);
  const user = clean(e.USER, 'posterchan');
  const host = clean(os.hostname(), 'posterchanos');

  const KEY = rgb(e, 132, 122, 168, 245);      // the label column: present, never competing
  const VAL = rgb(e, 226, 232, 255, 252);
  const ACC = rgb(e, 0, 240, 255, 51);
  const HOT = rgb(e, 255, 79, 216, 213);

  const who = `${BOLD}${HOT}${user}${KEY}@${BOLD}${ACC}${host}${R}`;
  const rule = `${KEY}${'─'.repeat(Math.max(8, Math.min(46, width - (wide ? LOGO_W + 2 : 0) - 2,
                                              user.length + host.length + 24)))}${R}`;
  const fact = (k, v) => `${KEY}${k.padEnd(8)}${R}${VAL}${v}${R}`;

  /* WHAT IS LEFT FOR A VALUE, once the logo column and the label column are spent. The meters are
   * the only rows whose width is a CHOICE, so they are the ones that have to make it — at 40
   * columns a fixed 12-cell bar pushed `ram` and `disk` past the edge and wrapped them, which is
   * the one thing this banner must never do. Below a floor there is no honest bar to draw and the
   * numbers alone are better than three cells pretending to be a gauge. */
  const gauge = (used, total) => {
    const text = `${human(used)} / ${human(total)}`;
    const avail = width - (wide ? LOGO_W + 2 : 0) - 8 - text.length - 2;
    const mw = Math.min(12, avail - 5);          // 5 = the space and the " 99%" beside the bar
    return mw >= 6 ? `${meter(e, used, total, mw)}  ${VAL}${text}${R}`
                   : `${VAL}${text}${R} ${rgb(e, 132, 122, 168, 245)}(${total > 0 ? Math.round(used / total * 100) : 0}%)${R}`;
  };

  const facts = [
    who,
    rule,
    fact('os', osName()),
    fact('kernel', `${clean(os.release())} · ${clean(os.arch())}`),
    fact('uptime', duration(os.uptime())),
    fact('cpu', `${cpu}${cpus.length ? ` · ${cpus.length} threads` : ''}`),
    `${KEY}${'ram'.padEnd(8)}${R}${gauge(usedMem, os.totalmem())}`,
    fact('gpu', gpu()),
    disk ? `${KEY}${'disk'.padEnd(8)}${R}${gauge(disk.used, disk.total)}`
         : fact('disk', 'not reported'),
    fact('network', network()),
    fact('session', `${clean(e.XDG_SESSION_TYPE, 'tty')} · ${clean(e.SHELL, '/bin/bash')}`),
  ];

  /* The neofetch signature, and it earns its place: it is the fastest way to see that this terminal
   * renders the colours the rest of the shell is about to use. */
  const swatch = (() => {
    const n = 8;
    let a = '', b = '';
    for (let i = 0; i < n; i++) { a += `${ramp(e, i, n)}███${R}`; b += `${ramp(e, n - 1 - i, n)}▀▀▀${R}`; }
    return [a, b];
  })();

  const lines = [];
  /* NARROW TABS DROP THE LOGO RATHER THAN WRAPPING. A banner that wraps is not a banner, it is
   * nineteen columns of debris in front of the first prompt — and a terminal pane can be any width
   * on a tiling desktop. The facts are what somebody actually reads, so they are what survives. */
  for (let i = 0; i < facts.length; i++) {
    const art = wide ? `${ramp(e, i, LOGO.length)}${LOGO[i] || ' '.repeat(LOGO_W)}${R}  ` : '';
    lines.push(art + facts[i]);
  }
  const pad = wide ? ' '.repeat(LOGO_W + 2) : '';
  lines.push('');
  lines.push(pad + swatch[0]);
  lines.push(pad + swatch[1]);
  lines.push('');
  lines.push(`${pad}${HOT}POSTERCHAN ${KEY}//${R} ${ACC}OWN YOUR SIGNAL${R}`);
  return `\r\n${lines.join('\r\n')}\r\n\r\n`;
}

module.exports = { render, human, duration, meter, pciModel, LOGO };
