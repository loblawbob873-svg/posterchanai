'use strict';
/* REMOVABLE DRIVES FOR FILES ON POSTERCHANOS: see them, mount them, read and write them, eject them.
 *
 * "important, we need to make sure files can see/mount/read/write removeable drives like USB". Files
 * already listed whatever was MOUNTED under /run/media/<user> -- but nothing on PosterChanOS mounted a
 * stick when it was plugged in (no udisks), so a USB drive simply never appeared.
 *
 * udisks2 does the mounting: it mounts AS THE LOGGED-IN USER under /run/media/<user>/<label> (so the
 * drive is readable and writable by them with no sudo), picks the kernel driver (vfat, exfat, ntfs3),
 * and is what every Linux desktop uses. The polkit rule shipped with posterchanos-shell lets the
 * session user mount, unmount and power off removable drives without a password.
 *
 * Safety: only REMOVABLE block devices with a filesystem are ever offered or accepted (USB, `rm`, or
 * hotplug) -- a page cannot name /dev/nvme0n1p2 and have it mounted -- and every command is run with
 * execFile (no shell), the device path taken from our own listing.
 *
 * `run(cmd, args)` is injected so tests/test_desktop_drives.py drives this with recorded output. */
const { execFile } = require('child_process');

function defaultRun(cmd, args) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { timeout: 20000, maxBuffer: 4 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) { err.stderr = String(stderr || ''); return reject(err); }
      resolve(String(stdout || ''));
    });
  });
}

const LSBLK = ['-J', '-b', '-o', 'NAME,PATH,PKNAME,TYPE,RM,HOTPLUG,TRAN,SIZE,FSTYPE,LABEL,MOUNTPOINTS,VENDOR,MODEL'];

function flatten(nodes, parent, out) {
  for (const n of nodes || []) {
    out.push(Object.assign({}, n, { _parent: parent || null }));
    if (n.children) flatten(n.children, n, out);
  }
  return out;
}

const truthy = v => v === true || v === 1 || v === '1' || v === 'true';

/* Removable filesystems: a partition (or a whole un-partitioned stick) on a removable/USB/hotplug disk. */
function parse(json) {
  let data;
  try { data = JSON.parse(json); } catch (_) { return []; }
  const all = flatten(data.blockdevices || [], null, []);
  const out = [];
  for (const n of all) {
    if (!n.fstype || !n.path || !/^\/dev\/[A-Za-z0-9._-]+$/.test(n.path)) continue;
    if (n.type !== 'part' && n.type !== 'disk') continue;
    const disk = n.type === 'disk' ? n : (n._parent || {});
    const removable = truthy(n.rm) || truthy(n.hotplug) || truthy(disk.rm) || truthy(disk.hotplug)
      || String(disk.tran || n.tran || '').toLowerCase() === 'usb';
    if (!removable || /^(swap|crypto_LUKS|LVM2_member|linux_raid_member)$/.test(n.fstype)) continue;
    const mounts = (n.mountpoints || []).filter(Boolean);
    const vendor = String(disk.vendor || '').trim(), model = String(disk.model || '').trim();
    out.push({
      dev: n.path, disk: disk.path || n.path, fstype: n.fstype, size: Number(n.size) || 0,
      label: String(n.label || '').trim() || [vendor, model].filter(Boolean).join(' ') || n.name,
      mountpoint: mounts[0] || '',
    });
  }
  return out;
}

function createDrives(opts) {
  const run = (opts && opts.run) || defaultRun;
  async function list() {
    try { return parse(await run('lsblk', LSBLK)); } catch (_) { return []; }
  }
  async function find(dev) {
    const d = (await list()).find(x => x.dev === String(dev || ''));
    if (!d) throw new Error('not a removable drive');
    return d;
  }
  /* Mount and say WHERE: udisksctl answers "Mounted /dev/sdb1 at /run/media/alice/STICK". */
  async function mount(dev) {
    const d = await find(dev);
    if (d.mountpoint) return { dev: d.dev, path: d.mountpoint, already: true };
    let out = '';
    try { out = await run('udisksctl', ['mount', '-b', d.dev, '--no-user-interaction']); }
    catch (e) { throw new Error(why(e, 'could not mount ' + d.label)); }
    const m = /\bat\s+(\/\S.*?)\.?\s*$/m.exec(out);
    const again = (await list()).find(x => x.dev === d.dev);
    const path = (m && m[1].trim()) || (again && again.mountpoint) || '';
    if (!path) throw new Error('mounted, but its folder could not be found');
    return { dev: d.dev, path };
  }
  async function unmount(dev) {
    const d = await find(dev);
    if (!d.mountpoint) return { dev: d.dev, unmounted: true };
    try { await run('udisksctl', ['unmount', '-b', d.dev, '--no-user-interaction']); }
    catch (e) { throw new Error(why(e, d.label + ' is busy -- close the files open on it and try again')); }
    return { dev: d.dev, unmounted: true };
  }
  /* Safe removal: unmount every filesystem on the stick, then power it off. */
  async function eject(dev) {
    const d = await find(dev);
    for (const p of (await list()).filter(x => x.disk === d.disk && x.mountpoint)) {
      try { await run('udisksctl', ['unmount', '-b', p.dev, '--no-user-interaction']); }
      catch (e) { throw new Error(why(e, p.label + ' is busy -- close the files open on it and try again')); }
    }
    try { await run('udisksctl', ['power-off', '-b', d.disk, '--no-user-interaction']); } catch (_) { /* unmounted is safe */ }
    return { dev: d.dev, ejected: true };
  }
  return { list, mount, unmount, eject };
}

function why(e, fallback) {
  const s = String((e && e.stderr) || (e && e.message) || '');
  if (/ENOENT|not found/i.test(String(e && e.code)) || /udisksctl: (command )?not found/i.test(s)) {
    return 'drive mounting is not installed on this computer (udisks) -- run the PosterChanOS update';
  }
  if (/NotAuthorized/i.test(s)) return 'not allowed to mount drives (polkit) -- run the PosterChanOS update';
  if (/busy/i.test(s)) return fallback;
  const line = s.split('\n').map(x => x.trim()).filter(Boolean).pop();
  return line ? line.replace(/^Error\S*:\s*/, '').slice(0, 200) : fallback;
}

module.exports = { createDrives, parse };
