/* USB passthrough for "This computer" (qemu:///session) — the desktop half of app/services/vmhost/usb.py.
 *
 * WHO OPENS THE DEVICE IS THE WHOLE STORY HERE. On a server host (qemu:///system) libvirt runs as root and chowns
 * /dev/bus/usb/BBB/DDD to the qemu user for as long as the VM holds it. A SESSION libvirt runs as you, has no dynamic
 * ownership and can chown nothing: QEMU opens the node itself, as you, so the node must already be read-write for
 * your account. Distributions ship those nodes root-owned 0664 (nas.lan: root:usb 0664). The grant is per DEVICE and
 * per ATTACH: `sudo -n /usr/local/bin/pc-usb-grant grant BUS DEV` (PosterChanOS ships it with a sudoers rule for
 * exactly that) gives the caller an ACL on that one node, after refusing hubs and anything the host uses; detaching
 * revokes it, and unplugging removes the node and the ACL with it. A blanket udev `uaccess` rule was the first design
 * and is gone: it handed the seat raw usbfs on every USB disk plugged in — root in all but name.
 *
 * And QEMU must HAVE the device model: Gentoo builds app-emulation/qemu with USE=-usb, which leaves out `usb-host`
 * (measured on nas.lan — the first real attach died inside QEMU). Asked of the binary, like qemu3d in vm.js.
 *
 * Same rules as the server: no hubs, no root hubs, never the disk this computer boots from (followed through
 * md/dm holders); a device the host has mounted is refused with where; one VM at a time; ids validated
 * (^[0-9a-f]{4}$), XML built here, never taken from the page; every attach and detach READ BACK from virsh.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const HEX4 = /^[0-9a-f]{4}$/;
const SYSTEM_MOUNTS = new Set(['/', '/boot', '/boot/efi', '/efi', '/usr', '/var']);
const GRANT = () => process.env.PC_USB_GRANT_BIN || '/usr/local/bin/pc-usb-grant';
const SWAPS = () => process.env.PC_SWAPS || '/proc/swaps';

const SYS = () => process.env.PC_SYS_ROOT || '/sys';
const DEV = () => process.env.PC_DEV_USB || '/dev/bus/usb';
const MOUNTINFO = () => process.env.PC_MOUNTINFO || '/proc/self/mountinfo';
const rd = p => { try{ return fs.readFileSync(p, 'utf8').trim(); }catch(_){ return ''; } };
const ls = p => { try{ return fs.readdirSync(p).sort(); }catch(_){ return null; } };
const real = p => { try{ return fs.realpathSync(p); }catch(_){ return p; } };
const clean = s => String(s || '').replace(/[^\x20-\x7e]/g, '').replace(/\s+/g, ' ').trim().slice(0, 64);

/** IFF_UP from `flags`, not operstate — "unknown" is what many interfaces that ARE up report. */
function ifaceUp(net, i){
  const f = parseInt(rd(path.join(net, i, 'flags')), 16);
  return Number.isFinite(f) ? !!(f & 1) : rd(path.join(net, i, 'operstate')) !== 'down';
}
function zpoolStatus(){
  if(process.env.PC_ZPOOL_STATUS != null) return process.env.PC_ZPOOL_STATUS;
  try{ return require('child_process').execFileSync('zpool', ['status', '-P'], { timeout: 10000, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }); }
  catch(_){ return ''; }
}
/** {block name: [mountpoints|'swap']} plus `unresolved` — the same rules as app/services/vmhost/usb.py `_mounts`:
 *  the SOURCE path, the major:minor field via /sys/dev/block (/dev/root), stat('/').dev for an anonymous btrfs
 *  root, every member of a multi-device btrfs, ZFS vdevs from `zpool status -P`, and /proc/swaps. A system mount
 *  none of these can place is `unresolved`, and the scan then refuses every disk. */
function mounts(){
  const out = {}; const unresolved = [];
  const block = path.join(SYS(), 'class', 'block');
  const known = n => !!n && fs.existsSync(path.join(block, n));
  const byPath = src => src.startsWith('/dev/') ? path.basename(real(src)) : '';
  const byMajmin = mm => /^\d+:\d+$/.test(mm || '') && !mm.startsWith('0:') ? path.basename(real(path.join(SYS(), 'dev', 'block', mm))) : '';
  const add = (n, mp) => { (out[n] = out[n] || []); if(!out[n].includes(mp)) out[n].push(mp); };
  const btrfsLeft = [], zfs = {};
  let table = null;
  try{ table = fs.readFileSync(MOUNTINFO(), 'utf8'); }catch(_){ unresolved.push('/'); }   // no table: fail closed
  for(const line of (table || '').split('\n')){
    const [left, right] = line.split(' - ');
    if(!right) continue;
    const L = left.split(' '), R = right.split(' ');
    const mm = L[2] || '', mp = (L[4] || '').replace(/\\040/g, ' '), fstype = R[0] || '', src = R[1] || '';
    const srcs = fstype === 'bcachefs' ? src.split(':') : [src];              // bcachefs names every member
    const names = new Set(srcs.map(byPath).concat([byMajmin(mm)]).filter(known));
    if(!names.size && mp === '/'){
      try{ const st = fs.statSync(process.env.PC_ROOT_PATH || '/'); const d = Number(process.env.PC_ROOT_DEV || st.dev);
        const n = byMajmin(`${Math.floor(d / 256) & 0xfff}:${(d & 0xff) | ((Math.floor(d / 1048576) & 0xfff) << 8)}`);
        if(known(n)) names.add(n); }catch(_){}
    }
    names.forEach(n => add(n, mp));
    if(fstype === 'zfs') (zfs[src.split('/')[0]] = zfs[src.split('/')[0]] || []).push(mp);
    else if(!names.size && (fstype === 'btrfs' || fstype === 'bcachefs')) btrfsLeft.push(mp);
    else if(!names.size && (src.startsWith('/dev/') || /^(ext[234]|xfs|f2fs|vfat|bcachefs|jfs|reiserfs|ntfs3)$/.test(fstype))) unresolved.push(mp);
  }
  const fsdir = path.join(SYS(), 'fs', 'btrfs');
  const members = {};
  for(const f of (ls(fsdir) || [])){ const d = ls(path.join(fsdir, f, 'devices')); if(d) members['btrfs:' + f] = d; }
  const bdir = path.join(SYS(), 'fs', 'bcachefs');
  for(const f of (ls(bdir) || [])){
    const d = (ls(path.join(bdir, f)) || []).filter(x => x.startsWith('dev-')).map(x => path.basename(real(path.join(bdir, f, x, 'block'))));
    if(d.length) members['bcachefs:' + f] = d;
  }
  const nfs = Object.keys(members).length;
  for(const devs of Object.values(members)){
    let mps = [...new Set(devs.flatMap(d => (out[d] || []).filter(m => m !== 'swap')))];
    if(btrfsLeft.length && (!mps.length || nfs === 1)) mps = [...new Set(mps.concat(btrfsLeft))];
    devs.forEach(d => mps.forEach(m => add(d, m)));
  }
  if(btrfsLeft.length && !nfs) unresolved.push(...btrfsLeft);
  // every vdev of every IMPORTED pool is in use, mounted or not; a mounted dataset lives on all of its pool's vdevs
  const vdevs = {}; let pool = '';
  for(const line of zpoolStatus().split('\n')){
    const m = line.match(/^\s*pool:\s*(\S+)/); if(m){ pool = m[1]; continue; }
    const tok = line.trim().split(/\s+/)[0] || '';
    if(pool && tok.startsWith('/dev/')) (vdevs[pool] = vdevs[pool] || []).push(byPath(tok));
  }
  for(const [p, ds] of Object.entries(vdevs)) ds.forEach(d => (zfs[p] || [`ZFS pool ${p}`]).forEach(m => add(d, m)));
  for(const [p, mps] of Object.entries(zfs)) if(!(vdevs[p] || []).length) unresolved.push(...mps);
  for(const line of rd(SWAPS()).split('\n').slice(1)){
    const f = line.trim().split(/\s+/)[0] || '';
    if(f.startsWith('/dev/')) add(byPath(f), 'swap');
  }
  out.__unresolved = unresolved.filter(m => SYSTEM_MOUNTS.has(m));
  return out;
}
function uses(block, name, m, depth){
  if(depth > 8) return [];
  const out = (m[name] || []).map(mp => [mp, '']);
  for(const h of (ls(path.join(block, name, 'holders')) || [])){
    const sub = uses(block, h, m, depth + 1);
    const label = rd(path.join(block, h, 'dm', 'name')) || h;
    if(sub.length) sub.forEach(([mp, via]) => out.push([mp, via || label])); else out.push(['', label]);
  }
  return out;
}
/** Every USB device on this computer that is not a hub/root hub/boot disk: [{vendor, product, bus, device, label,
 *  busy, access}]. `access` is false when this account cannot open the device node (see the header). */
function scan(){
  const base = path.join(SYS(), 'bus', 'usb', 'devices');
  const names = ls(base);
  if(!names) return null;
  const block = path.join(SYS(), 'class', 'block');
  const blocks = (ls(block) || []).map(b => [b, real(path.join(block, b))]);
  const m = mounts();
  const out = [];
  for(const n of names){
    if(n.includes(':') || n.startsWith('usb')) continue;
    const d = path.join(base, n);
    const bus = rd(path.join(d, 'busnum')), dev = rd(path.join(d, 'devnum'));
    const vendor = rd(path.join(d, 'idVendor')).toLowerCase(), product = rd(path.join(d, 'idProduct')).toLowerCase();
    if(!/^\d+$/.test(bus) || !/^\d+$/.test(dev) || !HEX4.test(vendor) || !HEX4.test(product)) continue;
    let cls = rd(path.join(d, 'bDeviceClass')).toLowerCase();
    const icls = names.filter(x => x.startsWith(n + ':')).map(x => rd(path.join(base, x, 'bInterfaceClass')).toLowerCase()).filter(Boolean);
    if(cls === '09' || icls.includes('09')) continue;                        // a hub takes everything behind it
    if((cls === '' || cls === '00') && icls.length) cls = icls[0];
    const r = real(d) + path.sep;
    let system = false, busy = '';
    for(const [b, br] of blocks){
      if(!br.startsWith(r)) continue;
      for(const [mp, via] of uses(block, b, m, 0)){
        if(SYSTEM_MOUNTS.has(mp)) system = true;
        if(!busy) busy = mp ? (mp === 'swap' ? `this computer uses ${b} as swap` : mp.startsWith('ZFS pool ') ? `${b} is a member of ${mp} on this computer` : `this computer has it mounted at ${mp}` + (via ? ` (through ${via})` : ''))
          : `this computer is using ${b} (part of ${via})`;
      }
    }
    if(system) continue;                                                      // never the disk this computer runs from
    if(!busy && m.__unresolved.length && blocks.some(([, br]) => br.startsWith(r)))
      busy = 'this computer could not tell which disk ' + m.__unresolved.join(', ') + ' is on, so no disk is given to a VM';
    if(!busy){
      const net = path.join(SYS(), 'class', 'net');
      const up = (ls(net) || []).find(i => real(path.join(net, i)).startsWith(r) && ifaceUp(net, i));
      if(up) busy = `this computer's network interface ${up} is up on it`;
    }
    const b = Number(bus), dv = Number(dev);
    const node = path.join(DEV(), String(b).padStart(3, '0'), String(dv).padStart(3, '0'));
    let access = true;
    try{ fs.accessSync(node, fs.constants.R_OK | fs.constants.W_OK); }catch(_){ access = false; }
    const who = [clean(rd(path.join(d, 'manufacturer'))), clean(rd(path.join(d, 'product')))].filter(Boolean).join(' ') || 'USB device';
    out.push({ vendor, product, bus: b, device: dv, class: cls, label: `${who} (${vendor}:${product})`, busy, access, node });
  }
  return out;
}

/** The <hostdev> entries of a definition: [{vendor, product, bus, device}]. */
function hostdevs(xml){
  const out = [];
  for(const m of String(xml || '').matchAll(/<hostdev\b[^>]*type=['"]usb['"][^>]*>([\s\S]*?)<\/hostdev>/g)){
    const body = m[1];
    const id = t => { const x = body.match(new RegExp('<' + t + "\\s+id=['\"]0x([0-9a-fA-F]{1,4})['\"]")); return x ? x[1].toLowerCase().padStart(4, '0') : null; };
    const a = body.match(/<address\s+[^>]*bus=['"](\w+)['"][^>]*device=['"](\w+)['"]/);
    out.push({ vendor: id('vendor'), product: id('product'), bus: a ? Number(a[1]) : null, device: a ? Number(a[2]) : null });
  }
  return out;
}
const matches = (e, d) => {
  const addr = e.bus != null && e.device != null, ids = !!(e.vendor && e.product);
  if(!addr && !ids) return false;
  if(ids && (e.vendor !== d.vendor || e.product !== d.product)) return false;
  if(addr && d.bus != null && (e.bus !== d.bus || e.device !== d.device)) return false;
  return true;
};
function hostdevXml(vendor, product, bus, device, optional){
  if(!HEX4.test(vendor) || !HEX4.test(product)) throw new Error('bad ids');
  const addr = Number.isInteger(bus) && Number.isInteger(device) && bus > 0 && device > 0 && bus < 1000 && device < 1000
    ? `<address bus="${bus}" device="${device}"/>` : '';
  return `<hostdev mode="subsystem" type="usb" managed="yes"><source${optional ? ' startupPolicy="optional"' : ''}>` +
         `<vendor id="0x${vendor}"/><product id="0x${product}"/>${addr}</source></hostdev>`;
}
function parseSpec(o){
  const vendor = o && o.vendor, product = o && o.product;
  if(typeof vendor !== 'string' || typeof product !== 'string' || !HEX4.test(vendor) || !HEX4.test(product))
    return { error: 'vendor and product must each be four lowercase hex digits' };
  const bus = o.bus == null ? null : o.bus, device = o.device == null ? null : o.device;
  if((bus == null) !== (device == null)) return { error: 'give both bus and device, or neither' };
  for(const v of [bus, device]) if(v != null && !(Number.isInteger(v) && v >= 1 && v <= 999)) return { error: 'bus/device must be whole numbers' };
  return { vendor, product, bus, device };
}

function make({ virsh, cleanName, root, qemuHas, run }){
  const running = st => /running|idle|blocked|paused/.test(String(st || '').toLowerCase());
  async function xmls(name){
    const info = await virsh(['dominfo', name]);
    if(!info.ok) return info;
    const st = ((info.out.match(/^State:\s*(.*)$/mi) || [])[1] || '').trim();
    const saved = await virsh(['dumpxml', name, '--inactive']);
    if(!saved.ok) return saved;
    const live = running(st) ? await virsh(['dumpxml', name]) : null;
    return { ok: true, state: st, running: running(st), saved: saved.out, live: live && live.ok ? live.out : null,
             autostart: /^Autostart:\s*enable/mi.test(info.out) };
  }
  async function owners(){
    const r = await virsh(['list', '--all', '--name']);
    const out = [];
    for(const n of (r.ok ? r.out.split(/\r?\n/).map(x => x.trim()).filter(Boolean) : [])){
      const x = await xmls(n);
      if(!x.ok) continue;
      for(const e of [...hostdevs(x.saved), ...hostdevs(x.live)]) out.push([e, n]);
    }
    return out;
  }
  async function checks(){
    const has = await qemuHas('usb-host');
    const out = [{ id: 'qemu-usb', ok: has !== false,
      label: has === false ? 'QEMU has no USB passthrough (usb-host is missing)' : 'QEMU supports USB passthrough (usb-host)',
      fix: has === false ? "This computer's QEMU was built without USB passthrough. PosterChanOS: update the system (it builds app-emulation/qemu with USE=usb). Other Gentoo: add 'app-emulation/qemu usb' to /etc/portage/package.use and run emerge --oneshot --changed-use app-emulation/qemu (as root)." : '' }];
    return out;
  }
  const accessFix = d => `Your account cannot open ${d.node}: a virtual machine on "This computer" runs as you, and QEMU ` +
    `must open the device itself. PosterChanOS grants one device at a time with pc-usb-grant (${GRANT()}, through sudo); this computer ` +
    'does not have it, or it refused. Elsewhere, give your account read-write access to that one device (as root: ' +
    `setfacl -m u:$USER:rw ${d.node}) — never a rule for every USB device, which would expose every USB disk.`;
  const helper = () => { try{ fs.accessSync(GRANT(), fs.constants.X_OK); return true; }catch(_){ return false; } };
  const canOpen = node => { try{ fs.accessSync(node, fs.constants.R_OK | fs.constants.W_OK); return true; }catch(_){ return false; } };
  /** Ask the privileged helper for THIS device only; the helper repeats every check itself. */
  async function grant(verb, bus, device){
    if(!helper()) return { ok: false, error: `${GRANT()} is not installed` };
    const r = await run('sudo', ['-n', GRANT(), verb, String(bus), String(device)], 20000);
    return r.ok ? { ok: true } : { ok: false, error: r.error || 'the grant was refused' };
  }
  async function list(){
    const devs = scan();
    if(!devs) return { ok: false, error: 'this computer has no USB bus' };
    const own = await owners();
    return { ok: true, checks: await checks(), devices: devs.map(d => {
      const o = own.find(([e]) => matches(e, d));
      return Object.assign({}, d, { used_by: o ? { uuid: o[1], name: o[1] } : null,
        fix: d.access ? '' : accessFix(d) });
    }) };
  }
  async function vmDevices(name){
    const x = await xmls(name);
    if(!x.ok) return [];
    const devs = scan() || [];
    const seen = {};
    const loose = e => e.vendor + ':' + e.product;
    const keyOf = e => loose(e) + (e.bus != null && e.device != null ? `@${e.bus}-${e.device}` : '');
    const view = (e, persistent) => { const host = devs.find(d => matches(e, d));
      return { kind: 'usb', vendor: e.vendor, product: e.product, bus: e.bus, device: e.device,
               label: host ? host.label : `USB device (${e.vendor}:${e.product})`, present: !!host, key: keyOf(e),
               live: false, persistent }; };
    for(const e of hostdevs(x.saved)) seen[keyOf(e)] = view(e, true);
    for(const e of hostdevs(x.live)){
      let k = keyOf(e);
      if(!seen[k] && seen[loose(e)] && !seen[loose(e)].live) k = loose(e);
      const cur = seen[k] = seen[k] || view(e, false);
      cur.live = true;
      if(e.bus != null && cur.bus == null){ cur.bus = e.bus; cur.device = e.device; }
    }
    return Object.values(seen);
  }
  async function writeXml(name, body){
    const dir = path.join(root(), name);
    await fs.promises.mkdir(dir, { recursive: true, mode: 0o700 });
    const file = path.join(dir, `usb-${process.pid}-${Date.now()}.xml`);
    await fs.promises.writeFile(file, body, { mode: 0o600 });
    return file;
  }
  async function change(verb, name, body, live, config){
    const file = await writeXml(name, body);
    try{
      return await virsh([verb, name, file].concat(live ? ['--live'] : [], config ? ['--config'] : []), 60000);
    }finally{ try{ await fs.promises.unlink(file); }catch(_){} }
  }
  async function attach(name, opts){
    name = cleanName(name); if(!name) return { ok: false, error: 'invalid VM name' };
    const spec = parseSpec(opts); if(spec.error) return { ok: false, error: spec.error, code: 'bad_request' };
    const persist = opts && opts.persist === false ? false : true;
    const bad = (await checks()).find(c => !c.ok);
    if(bad) return { ok: false, code: 'unsupported', error: bad.label + '. ' + bad.fix };
    const x = await xmls(name); if(!x.ok) return x;
    if(x.autostart) return { ok: false, code: 'conflict', error: 'this VM starts on its own when you sign in (autostart), which would take the device without the checks a start from here makes — turn "Start with the host" off in Settings first' };
    if(!x.running && !persist) return { ok: false, code: 'bad_request', error: 'the VM is not running — a device added now is kept in its settings' };
    const devs = scan();
    if(!devs) return { ok: false, code: 'unsupported', error: 'this computer has no USB bus' };
    const found = devs.filter(d => d.vendor === spec.vendor && d.product === spec.product && (spec.bus == null || (d.bus === spec.bus && d.device === spec.device)));
    if(!found.length) return { ok: false, code: 'not_found', error: 'that device is not plugged into this computer (any more)' };
    if(found.length > 1) return { ok: false, code: 'bad_request', error: 'more than one device has these ids — name it by bus and device too' };
    const d = found[0];
    if(d.busy) return { ok: false, code: 'conflict', error: `${d.label} is in use by this computer: ${d.busy}` };
    // who else has it is asked BEFORE anything is granted, so a refusal leaves nothing behind
    for(const [e, owner] of await owners()){
      if(matches(e, d)) return { ok: false, code: 'conflict', error: owner === name ? `${d.label} is already attached to this VM` : `${d.label} is already attached to the VM ${owner}` };
    }
    let granted = false;
    const fail = async res => { if(granted) await grant('revoke', d.bus, d.device); return res; };
    if(!d.access){
      const g = await grant('grant', d.bus, d.device);
      granted = g.ok;
      if(!g.ok || !canOpen(d.node)) return fail({ ok: false, code: 'forbidden', error: accessFix(d) + (g.error ? ` (${g.error})` : '') });
      d.access = true;
    }
    const twins = devs.filter(t => t.vendor === d.vendor && t.product === d.product).length > 1;
    const body = hostdevXml(d.vendor, d.product, twins ? d.bus : null, twins ? d.device : null, true);
    const live = x.running, config = persist || !x.running;
    const r = await change('attach-device', name, body, live, config);
    if(!r.ok) return fail(Object.assign({ code: /in use/i.test(r.error) ? 'conflict' : 'backend_error' }, r));
    const after = await xmls(name);
    const miss = [];
    if(live && !hostdevs(after.live).some(e => matches(e, d))) miss.push('the running VM');
    if(config && !hostdevs(after.saved).some(e => matches(e, d))) miss.push('its saved settings');
    if(miss.length){
      for(const [lv, cf] of [[live, false], [false, config]]) if(lv || cf) await change('detach-device', name, body, lv, cf);
      return fail({ ok: false, code: 'backend_error', error: 'libvirt accepted the attach but it is not in ' + miss.join(' or ') + ' — what landed was removed again' });
    }
    return { ok: true, devices: await vmDevices(name) };
  }
  /** Before a VM starts: every saved USB device must still be safe to hand over — the host may have mounted the
   *  stick, or turned the adapter into its uplink, since it was attached. Refuses (and takes back the grant). */
  async function startGuard(name){
    name = cleanName(name); if(!name) return { ok: false, error: 'invalid VM name' };
    const x = await xmls(name); if(!x.ok) return x;
    const saved = hostdevs(x.saved);
    if(!saved.length) return { ok: true };
    const devs = scan();
    if(!devs) return { ok: false, error: 'this VM has USB devices saved and this computer could not check them — detach them first' };
    for(const e of saved){
      for(const d of devs.filter(dd => matches(e, dd))){
        if(d.busy){
          if(helper()) await grant('revoke', d.bus, d.device);
          return { ok: false, error: `This VM's saved devices include ${d.label}, which this computer is using (${d.busy}). Detach it from the VM, or stop using it here, first.` };
        }
      }
    }
    return { ok: true };
  }
  async function hasDevices(name){
    const x = await xmls(cleanName(name)); return !!(x.ok && hostdevs(x.saved).length);
  }
  async function detach(name, opts){
    name = cleanName(name); if(!name) return { ok: false, error: 'invalid VM name' };
    const spec = parseSpec(opts); if(spec.error) return { ok: false, error: spec.error, code: 'bad_request' };
    const x = await xmls(name); if(!x.ok) return x;
    const hit = e => e.vendor === spec.vendor && e.product === spec.product &&
      (spec.bus == null || e.bus == null || (e.bus === spec.bus && e.device === spec.device));
    const liveHit = x.running ? hostdevs(x.live).find(hit) : null, savedHit = hostdevs(x.saved).find(hit);
    if(!liveHit && !savedHit) return { ok: false, code: 'not_found', error: 'that device is not attached to this VM' };
    if(liveHit){ const r = await change('detach-device', name, hostdevXml(liveHit.vendor, liveHit.product, liveHit.bus, liveHit.device, false), true, false); if(!r.ok) return r; }
    if(savedHit){ const r = await change('detach-device', name, hostdevXml(savedHit.vendor, savedHit.product, savedHit.bus, savedHit.device, false), false, true); if(!r.ok) return r; }
    // take back the per-device grant (best effort: an unplugged device took its node and ACL with it)
    const addr = [liveHit, savedHit].find(e => e && e.bus != null) || (scan() || []).find(d => hit(d));
    if(addr && helper()) await grant('revoke', addr.bus, addr.device);
    const after = await xmls(name);
    if((liveHit && hostdevs(after.live).some(hit)) || (savedHit && hostdevs(after.saved).some(hit)))
      return { ok: false, code: 'backend_error', error: 'libvirt accepted the detach but the device is still attached' };
    return { ok: true, devices: await vmDevices(name) };
  }
  return { list, attach, detach, vmDevices, startGuard, hasDevices };
}

module.exports = { make, scan, mounts, hostdevs, hostdevXml, parseSpec, matches };
