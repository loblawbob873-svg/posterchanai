/* USB passthrough for "This computer" (qemu:///session) — the desktop half of app/services/vmhost/usb.py.
 *
 * WHO OPENS THE DEVICE IS THE WHOLE STORY HERE. On a server host (qemu:///system) libvirt runs as root and chowns
 * /dev/bus/usb/BBB/DDD to the qemu user for as long as the VM holds it. A SESSION libvirt runs as you, has no dynamic
 * ownership and can chown nothing: QEMU opens the node itself, as you, so the node must already be read-write for
 * your account. Distributions ship those nodes root-owned 0664 (nas.lan: root:usb 0664), so without a grant every
 * attach fails. PosterChanOS installs /etc/udev/rules.d/70-posterchan-usb-passthrough.rules, which tags every
 * non-hub USB device `uaccess` — systemd-logind then gives the person at the seat an ACL on it, the same grant the
 * seat already gets for its sound card and camera. This module MEASURES that (fs.access on the node) and refuses with
 * the rule to install, instead of letting QEMU fail with a bare "Permission denied".
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
const RULE_FILE = '/etc/udev/rules.d/70-posterchan-usb-passthrough.rules';
const RULE = 'SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{bDeviceClass}!="09", TAG+="uaccess"';

const SYS = () => process.env.PC_SYS_ROOT || '/sys';
const DEV = () => process.env.PC_DEV_USB || '/dev/bus/usb';
const MOUNTINFO = () => process.env.PC_MOUNTINFO || '/proc/self/mountinfo';
const rd = p => { try{ return fs.readFileSync(p, 'utf8').trim(); }catch(_){ return ''; } };
const ls = p => { try{ return fs.readdirSync(p).sort(); }catch(_){ return null; } };
const real = p => { try{ return fs.realpathSync(p); }catch(_){ return p; } };
const clean = s => String(s || '').replace(/[^\x20-\x7e]/g, '').replace(/\s+/g, ' ').trim().slice(0, 64);

function mounts(){
  const out = {};
  for(const line of rd(MOUNTINFO()).split('\n')){
    const [left, right] = line.split(' - ');
    if(!right) continue;
    const src = (right.split(' ')[1] || '');
    if(!src.startsWith('/dev/')) continue;
    const name = path.basename(real(src));
    (out[name] = out[name] || []).push((left.split(' ')[4] || '').replace(/\\040/g, ' '));
  }
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
        if(!busy) busy = mp ? (mp === 'swap' ? `this computer uses ${b} as swap` : `this computer has it mounted at ${mp}` + (via ? ` (through ${via})` : ''))
          : `this computer is using ${b} (part of ${via})`;
      }
    }
    if(system) continue;                                                      // never the disk this computer runs from
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

function make({ virsh, cleanName, root, qemuHas }){
  const running = st => /running|idle|blocked|paused/.test(String(st || '').toLowerCase());
  async function xmls(name){
    const info = await virsh(['dominfo', name]);
    if(!info.ok) return info;
    const st = ((info.out.match(/^State:\s*(.*)$/mi) || [])[1] || '').trim();
    const saved = await virsh(['dumpxml', name, '--inactive']);
    if(!saved.ok) return saved;
    const live = running(st) ? await virsh(['dumpxml', name]) : null;
    return { ok: true, state: st, running: running(st), saved: saved.out, live: live && live.ok ? live.out : null };
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
  const accessFix = d => `Your account cannot open ${d.node}: a virtual machine on "This computer" runs as you, and QEMU must open the device itself. ` +
    `As root, create ${RULE_FILE} containing:\n${RULE}\nthen run: udevadm control --reload && udevadm trigger --subsystem-match=usb --action=change ` +
    '(PosterChanOS installs this rule; it gives the person signed in at this computer access to plugged-in USB devices, like their sound card).';

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
    for(const [src, xml] of [['persistent', x.saved], ['live', x.live]]){
      for(const e of hostdevs(xml)){
        const key = e.vendor + ':' + e.product;
        const host = devs.find(d => matches(e, d));
        const cur = seen[key] = seen[key] || { kind: 'usb', vendor: e.vendor, product: e.product, bus: e.bus, device: e.device,
          label: host ? host.label : `USB device (${e.vendor}:${e.product})`, present: !!host, key, live: false, persistent: false };
        cur[src] = true;
        if(e.bus != null && cur.bus == null){ cur.bus = e.bus; cur.device = e.device; }
      }
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
    if(!x.running && !persist) return { ok: false, code: 'bad_request', error: 'the VM is not running — a device added now is kept in its settings' };
    const devs = scan();
    if(!devs) return { ok: false, code: 'unsupported', error: 'this computer has no USB bus' };
    const found = devs.filter(d => d.vendor === spec.vendor && d.product === spec.product && (spec.bus == null || (d.bus === spec.bus && d.device === spec.device)));
    if(!found.length) return { ok: false, code: 'not_found', error: 'that device is not plugged into this computer (any more)' };
    if(found.length > 1) return { ok: false, code: 'bad_request', error: 'more than one device has these ids — name it by bus and device too' };
    const d = found[0];
    if(d.busy) return { ok: false, code: 'conflict', error: `${d.label} is in use by this computer: ${d.busy}` };
    if(!d.access) return { ok: false, code: 'forbidden', error: accessFix(d) };
    for(const [e, owner] of await owners()){
      if(matches(e, d)) return { ok: false, code: 'conflict', error: owner === name ? `${d.label} is already attached to this VM` : `${d.label} is already attached to the VM ${owner}` };
    }
    const twins = devs.filter(t => t.vendor === d.vendor && t.product === d.product).length > 1;
    const body = hostdevXml(d.vendor, d.product, twins ? d.bus : null, twins ? d.device : null, true);
    const live = x.running, config = persist || !x.running;
    const r = await change('attach-device', name, body, live, config);
    if(!r.ok) return Object.assign({ code: /in use/i.test(r.error) ? 'conflict' : 'backend_error' }, r);
    const after = await xmls(name);
    const miss = [];
    if(live && !hostdevs(after.live).some(e => matches(e, d))) miss.push('the running VM');
    if(config && !hostdevs(after.saved).some(e => matches(e, d))) miss.push('its saved settings');
    if(miss.length) return { ok: false, code: 'backend_error', error: 'libvirt accepted the attach but it is not in ' + miss.join(' or ') };
    return { ok: true, devices: await vmDevices(name) };
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
    const after = await xmls(name);
    if((liveHit && hostdevs(after.live).some(hit)) || (savedHit && hostdevs(after.saved).some(hit)))
      return { ok: false, code: 'backend_error', error: 'libvirt accepted the detach but the device is still attached' };
    return { ok: true, devices: await vmDevices(name) };
  }
  return { list, attach, detach, vmDevices };
}

module.exports = { make, scan, hostdevs, hostdevXml, parseSpec, matches, RULE, RULE_FILE };
