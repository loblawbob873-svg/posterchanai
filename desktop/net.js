/* Network and wifi for the PosterChan shell.
 *
 * WHY nmcli AND NOT D-BUS. NetworkManager is already in the OS build's package list, and its D-Bus
 * API is the richer interface — but it is also a large surface to bind from Node with no dependency,
 * and every call would be untestable on a machine with no system bus. `nmcli -t` is a stable,
 * documented, colon-separated contract that NetworkManager treats as an API, and stubbing one
 * executable is how this whole module gets tested on a box with no wifi hardware at all.
 *
 * THE FIELD SEPARATOR IS A COLON AND SSIDS CONTAIN COLONS. nmcli escapes them as `\:` in terse
 * mode, so a plain `split(':')` tears a network called "Cafe: Free" into two fields and shifts every
 * column after it — the security column becomes part of the name, the signal becomes the security,
 * and the row is quietly wrong rather than obviously broken. Splitting has to honour the escape.
 *
 * A PASSWORD IS NEVER AN ARGUMENT. `nmcli ... password <secret>` puts it in the process table, where
 * every other user on the machine can read it out of `ps` for as long as the connect takes. It goes
 * in on stdin instead, which is what `--ask` is for.
 */
'use strict';
const { execFile, spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const NMCLI = process.env.PC_NMCLI || 'nmcli';
const SUDO = process.env.PC_SUDO || 'sudo';

/** Split one terse nmcli row, honouring the `\:` escape (and `\\`). */
function fields(line){
  const out = [];
  let cur = '';
  for(let i = 0; i < line.length; i++){
    const c = line[i];
    if(c === '\\' && i + 1 < line.length){ cur += line[++i]; continue; }
    if(c === ':'){ out.push(cur); cur = ''; continue; }
    cur += c;
  }
  out.push(cur);
  return out;
}

function run(args, opts){ return exec(NMCLI, args, opts); }

function exec(cmd, args, opts){
  const o = opts || {};
  return new Promise((resolve, reject) => {
    const child = execFile(cmd, args, { timeout: o.timeout || 45000, maxBuffer: 4 << 20 },
      (err, stdout, stderr) => {
        if(err){
          /* nmcli says WHY on stderr and exits non-zero; the message is the only thing that can tell
           * "wrong password" from "no such network" from "the radio is off", and a caller handed a
           * bare exit code cannot tell a person anything useful. */
          const why = String(stderr || err.message || '').trim().split('\n').pop();
          const e = new Error(why || 'nmcli failed');
          /* AND IT IS WRITTEN DOWN. The page shows one toast; the shell's log is what somebody can read
           * back afterwards on a machine they cannot screenshot (a LiveUSB on borrowed hardware). The
           * arguments never hold a secret -- passwords travel on stdin -- so they are safe to log. */
          try{
            if(cmd === NMCLI) console.warn('[net] nmcli ' + args.join(' ') + ' failed: ' + (why || err.code || 'no message'));
            else console.warn('[net] ' + cmd + ' ' + args.join(' ') + ' failed: ' + (why || err.code || 'no message'));
          }catch(_){}
          e.code = err.code;
          return reject(e);
        }
        resolve(String(stdout || ''));
      });
    /* STDIN IS ALWAYS CLOSED, with or without a secret to send. Left open, any nmcli invocation
     * that reads it waits for input that is never coming and the call hangs until the timeout —
     * which on a shell means the wifi list simply never appears, with nothing to say why.
     *
     * AND EPIPE IS NOT AN ERROR HERE. nmcli can exit before it ever reads stdin — a rejected
     * password is exactly that, it refuses on the arguments alone — and writing to a pipe whose far
     * end has gone emits `error` ASYNCHRONOUSLY on the stream. An 'error' event with no listener is
     * re-thrown by Node and takes the whole process down, so the desktop shell would die on a wrong
     * wifi password. The try/catch around the write cannot help: it is not thrown from here.
     * The real failure is the exit code, which execFile already reports. */
    try{
      child.stdin.on('error', () => {});
      child.stdin.end(o.stdin == null ? '' : o.stdin);
    }catch(_){}
  });
}

const rows = (out) => String(out).split('\n').filter(Boolean).map(fields);

async function available(){
  try{ await run(['--version']); return true; }catch(_){ return false; }
}

/** Every device NetworkManager knows: wifi, ethernet, and what each is doing. */
async function devices(){
  const out = await run(['-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status']);
  return rows(out).map(([device, type, state, connection]) =>
    ({ device, type, state, connection: connection === '--' ? '' : connection }));
}

/* THE LIST IS DEDUPED BY SSID, KEEPING THE STRONGEST. A band-steering router publishes the same
 * network on 2.4 and 5 GHz and a mesh publishes it from every node, so a raw scan shows one name
 * five times — which reads as five networks to anyone who is not a network engineer. */
async function wifi(rescan){
  const args = ['-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY,FREQ', 'device', 'wifi', 'list'];
  if(rescan) args.push('--rescan', 'yes');
  const best = new Map();
  for(const [inUse, ssid, signal, security, freq] of rows(await run(args, { timeout: 60000 }))){
    if(!ssid) continue;                                   // a hidden network has no name to show
    const row = { ssid, signal: +signal || 0, secure: !!(security && security !== '--'),
                  security: security === '--' ? '' : security, band: /^5|^6/.test(freq) ? '5' : '2.4',
                  active: inUse === '*' };
    const had = best.get(ssid);
    if(!had || row.signal > had.signal || row.active) best.set(ssid, had && had.active ? had : row);
  }
  /* Coerced. `active` is always set here by construction, so this is not a live bug — but the same
   * comparator written against rows that lack the field returns NaN, which sorts as "no opinion",
   * and the network you are CONNECTED TO ends up somewhere in the middle of the list. */
  return [...best.values()].sort((a, b) =>
    ((b.active ? 1 : 0) - (a.active ? 1 : 0)) || ((b.signal || 0) - (a.signal || 0)));
}

/** Saved connections, so a known network can be joined without asking for the password again. */
async function saved(){
  const out = await run(['-t', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show']);
  return rows(out).map(([name, type, device]) => ({ name, type, device: device === '--' ? '' : device }));
}

/* CONNECTING TO A KNOWN NETWORK IS A DIFFERENT COMMAND, and getting that wrong is why a shell asks
 * for a password it already has. `device wifi connect` with no password re-uses the stored secret
 * only sometimes; `connection up` is the one that always does. */
async function connect(ssid, password){
  const known = (await saved()).some(c => c.name === ssid);
  if(known && !password){
    await run(['connection', 'up', 'id', ssid], { timeout: 60000 });
    return { ssid, reused: true };
  }
  /* A PASSWORD MEANS "JOIN FRESH", SO A STALE PROFILE MUST GO FIRST.
   *
   * A rejected `device wifi connect` still leaves a saved profile carrying the wrong secret. The
   * next attempt with a new password then activates THAT broken profile instead of trying the new
   * one, so a wrong first try can never be recovered by a correct second — and, reported from a TV
   * where the password was retyped many times, the saved PSK ended up as every attempt concatenated
   * into one very long string, which nmtui showed and which of course never authenticated. Deleting
   * the existing profile before a password join guarantees each attempt starts from nothing: the
   * secret nmcli stores is exactly what was typed this time, and retrying can only help.
   *
   * Only when a password was given (an unattended `connection up` reuse is handled above) and only
   * when one actually exists; a delete of a missing profile is a harmless nonzero we swallow. */
  if(password && known){
    try{ await run(['connection', 'delete', 'id', ssid], { timeout: 20000 }); }catch(_){}
  }
  /* `--ask` is a GLOBAL nmcli option and must precede the object. Appending it after the SSID
   * makes nmcli parse it as an argument to `device wifi connect` and reject every secured network
   * from the live USB. The secret still travels only over stdin, never through argv. */
  const args = password
    ? ['--ask', 'device', 'wifi', 'connect', ssid]
    : ['device', 'wifi', 'connect', ssid];
  await run(args, { timeout: 60000, stdin: password ? password + '\n' : undefined });
  return { ssid, reused: false };
}

async function disconnect(device){ await run(['device', 'disconnect', device]); return { device }; }

/** Forget a network entirely — the saved profile AND its stored secret. */
async function forget(ssid){ await run(['connection', 'delete', 'id', ssid]); return { ssid }; }

async function radio(on){
  await run(['radio', 'wifi', on === false ? 'off' : 'on']);
  return { wifi: on !== false };
}

/** What the shell puts in the corner: are we on, over what, and how good is it. */
async function status(){
  const [devs, nets] = await Promise.all([devices(), wifi(false).catch(() => [])]);
  const online = devs.find(d => d.state === 'connected' && d.type !== 'loopback');
  const active = nets.find(n => n.active);
  return {
    online: !!online,
    kind: online ? online.type : '',
    name: online ? online.connection : '',
    signal: active ? active.signal : 0,
    devices: devs,
  };
}

/* A CHANGE IS PUSHED, NOT POLLED. `nmcli monitor` prints a line per change and never exits, so the
 * shell learns about a dropped wifi the moment it drops rather than up to a poll-interval later —
 * and a laptop lid closing must not cost a timer that runs for ever either way. */
function monitor(onChange){
  const child = spawn(NMCLI, ['monitor'], { stdio: ['ignore', 'pipe', 'ignore'] });
  let buf = '';
  child.stdout.on('data', (c) => {
    buf += c;
    let i;
    while((i = buf.indexOf('\n')) >= 0){
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if(line) { try{ onChange(line); }catch(_){} }
    }
  });
  return () => { try{ child.kill(); }catch(_){} };
}

/* ------------------------------------------------------------------ bridges for virtual machines
 *
 * A BRIDGE IS WHAT PUTS A VM ON THE LAN: the ethernet card becomes a port of a software switch, the
 * machine's own address moves to the bridge, and a VM's adapter plugs into the same switch — so it gets
 * an address from the router like any other computer, instead of hiding behind libvirt's NAT.
 *
 * EVERY CHANGE GOES THROUGH `sudo -n nmcli`. Measured on both PosterChanOS machines: `nmcli general
 * permissions` answers `auth` for settings.modify.system and network-control, and the shell has no
 * polkit agent to answer that `auth`, so a plain `nmcli connection add` is refused. The account already
 * holds the NOPASSWD grant Printers and Displays use; `-n` makes a missing grant a quick, readable error
 * rather than a prompt nobody can see. argv arrays only, never a shell, and every name is validated here.
 *
 * TAKING THE CARD DROPS THE NETWORK FOR A MOMENT, and if the bridge never gets an address the machine is
 * OFFLINE — possibly a remote machine somebody is reaching over that very card. So creation is
 * transactional: if the bridge does not come up with an IPv4 address in BRIDGE_WAIT_MS, everything made
 * is deleted and the card's previous profile is switched back on and brought up. The bridge copies the
 * card's MAC, so a DHCP router hands it the SAME address the machine already had.
 *
 * A session VM (qemu:///session, this computer's VMs) joins a bridge through qemu-bridge-helper, which
 * libvirt runs on its behalf. Measured: the helper ships 0755 (not setuid) and /etc/qemu/bridge.conf
 * allows nothing, so with `allowVms` the bridge is also allowed there and the helper made setuid (what
 * Debian and Fedora ship by default; the ACL in bridge.conf is what limits it). Deleting a bridge leaves
 * its `allow` line in place: it names an interface that no longer exists (harmless), rewriting a
 * root-owned file to remove it is a second privileged edit for nothing, and a bridge re-created under the
 * same name works again at once. */
const BRIDGE_RE = /^[a-zA-Z][a-zA-Z0-9_-]{0,14}$/;
const IPV4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;
const MAC_RE = /^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/;
const BRIDGE_WAIT_MS = Number(process.env.PC_BRIDGE_WAIT_MS) || 30000;
const sysNet = () => process.env.PC_SYS_NET || '/sys/class/net';
const bridgeConf = () => process.env.PC_QEMU_BRIDGE_CONF || '/etc/qemu/bridge.conf';
const bridgeHelper = () => process.env.PC_QEMU_BRIDGE_HELPER || '/usr/libexec/qemu-bridge-helper';
const stateFile = () => process.env.PC_BRIDGE_STATE || path.join(os.homedir(), '.config', 'posterchanos', 'bridges.json');
const asRoot = (args, opts) => exec(SUDO, ['-n', NMCLI].concat(args), opts);

function readState(){ try{ return JSON.parse(fs.readFileSync(stateFile(), 'utf8')) || {}; }catch(_){ return {}; } }
function writeState(st){
  try{ fs.mkdirSync(path.dirname(stateFile()), { recursive: true }); fs.writeFileSync(stateFile(), JSON.stringify(st, null, 1)); }catch(_){}
}
function macOf(dev){
  try{ const m = fs.readFileSync(path.join(sysNet(), dev, 'address'), 'utf8').trim().toLowerCase(); return MAC_RE.test(m) ? m : ''; }
  catch(_){ return ''; }
}
function vmAllowed(name){
  let conf = '';
  try{ conf = fs.readFileSync(bridgeConf(), 'utf8'); }catch(_){ return false; }
  return conf.split('\n').some(l => { const t = l.trim().split(/\s+/); return t[0] === 'allow' && (t[1] === name || t[1] === 'all'); });
}
function helperSetuid(){ try{ return !!(fs.statSync(bridgeHelper()).mode & 0o4000); }catch(_){ return false; } }

/** Every connection with its uuid, type and device (terse, escape-aware). */
async function connections(){
  const out = await run(['-t', '-f', 'NAME,UUID,TYPE,DEVICE,ACTIVE', 'connection', 'show']);
  return rows(out).map(([name, uuid, type, device, active]) =>
    ({ name, uuid, type, device: device === '--' ? '' : device, active: active === 'yes' }));
}
/** connection.master / slave-type for a set of connections, in one call. */
async function masters(uuids){
  if(!uuids.length) return {};
  const out = await run(['-t', '-f', 'connection.uuid,connection.master,connection.slave-type', 'connection', 'show'].concat(uuids));
  const res = {}; let cur = null;
  for(const [k, v] of rows(out)){
    if(k === 'connection.uuid'){ cur = res[v] = { master: '', type: '' }; continue; }
    if(!cur) continue;
    if(k === 'connection.master') cur.master = v === '--' ? '' : v;
    if(k === 'connection.slave-type') cur.type = v === '--' ? '' : v;
  }
  return res;
}
async function ipv4Of(dev){
  if(!dev) return [];
  try{
    const out = await run(['-g', 'IP4.ADDRESS', 'device', 'show', dev]);
    return String(out).split(/\s*\|\s*|\n/).map(x => x.trim()).filter(x => /^\d+\.\d+\.\d+\.\d+\/\d+$/.test(x));
  }catch(_){ return []; }
}

async function bridges(){
  let devs, conns;
  try{ [devs, conns] = await Promise.all([devices(), connections()]); }
  catch(e){ return { available: false, error: String(e && e.message || e), bridges: [], nics: [] }; }
  const br = conns.filter(c => c.type === 'bridge');
  const ms = await masters(conns.filter(c => c.type !== 'bridge').map(c => c.uuid)).catch(() => ({}));
  const setuid = helperSetuid();
  const out = [];
  for(const c of br){
    const dev = c.device || c.name;
    const d = devs.find(x => x.device === dev);
    const external = !!(d && /externally/.test(d.state));
    const managed = /^virbr/.test(dev) ? 'libvirt' : external ? 'external' : 'nm';
    const ports = conns.filter(p => { const m = ms[p.uuid]; return m && m.type === 'bridge' && (m.master === c.uuid || m.master === c.name || m.master === dev); })
      .map(p => p.device || p.name);
    out.push({ name: dev, uuid: c.uuid, device: c.device, active: c.active || !!(d && /^connected/.test(d.state)),
               state: d ? d.state : 'disconnected', ipv4: await ipv4Of(c.device), ports, managed,
               vmReady: vmAllowed(dev) && setuid });
  }
  const nics = devs.filter(d => d.type === 'ethernet').map(d => ({ device: d.device, connection: d.connection,
    state: d.state, mac: macOf(d.device), port: out.some(b => b.ports.includes(d.device)) }));
  return { available: true, bridges: out, nics, helperSetuid: setuid };
}

function cleanSpec(spec){
  const s = spec || {};
  const name = String(s.name || '').trim();
  if(!BRIDGE_RE.test(name)) throw new Error('A bridge name is a letter followed by up to 14 letters, digits, - or _ (for example br0).');
  const mode = s.mode === 'static' ? 'static' : 'dhcp';
  const out = { name, nic: String(s.nic || '').trim(), mode, allowVms: s.allowVms !== false, address: '', gateway: '', dns: [] };
  if(mode === 'static'){
    const m = String(s.address || '').trim().match(/^([\d.]+)\/(\d{1,2})$/);
    if(!m || !IPV4.test(m[1]) || +m[2] < 1 || +m[2] > 32) throw new Error('Enter the address with its prefix, for example 192.168.1.50/24.');
    out.address = m[1] + '/' + (+m[2]);
    const gw = String(s.gateway || '').trim();
    if(gw && !IPV4.test(gw)) throw new Error('The gateway must be an IPv4 address.');
    out.gateway = gw;
    out.dns = String(s.dns || '').split(/[\s,]+/).filter(Boolean);
    if(out.dns.some(x => !IPV4.test(x))) throw new Error('DNS servers must be IPv4 addresses, separated by commas.');
  }
  return out;
}

async function waitForAddress(dev, ms){
  const until = Date.now() + ms;
  for(;;){
    if((await ipv4Of(dev)).length) return true;
    if(Date.now() >= until) return false;
    await new Promise(r => setTimeout(r, Math.min(1000, Math.max(50, until - Date.now()))));
  }
}

async function createBridge(spec){
  let s;
  try{ s = cleanSpec(spec); }catch(e){ return { ok: false, error: e.message }; }
  const [devs, conns] = await Promise.all([devices(), connections()]);
  const nic = devs.find(d => d.device === s.nic);
  if(!nic) return { ok: false, error: 'There is no network card called ' + s.nic + ' on this computer.' };
  if(nic.type !== 'ethernet') return { ok: false, error: 'Only a wired (ethernet) card can join a bridge — Wi-Fi cannot be bridged.' };
  if(devs.some(d => d.device === s.name) || conns.some(c => c.name === s.name || c.device === s.name))
    return { ok: false, error: 'Something called ' + s.name + ' already exists — choose another name.' };
  const mac = macOf(s.nic);
  const prev = conns.find(c => c.device === s.nic && c.type !== 'bridge') || null;
  const port = (s.name + '-port-' + s.nic).slice(0, 64);
  const ip = s.mode === 'static'
    ? ['ipv4.method', 'manual', 'ipv4.addresses', s.address].concat(s.gateway ? ['ipv4.gateway', s.gateway] : [])
        .concat(s.dns.length ? ['ipv4.dns', s.dns.join(',')] : [])
    : ['ipv4.method', 'auto'];
  const made = [];
  const rollback = async (why) => {
    for(const n of made.reverse()){ try{ await asRoot(['connection', 'delete', 'id', n], { timeout: 20000 }); }catch(_){} }
    if(prev){
      try{ await asRoot(['connection', 'modify', 'uuid', prev.uuid, 'connection.autoconnect', 'yes'], { timeout: 20000 }); }catch(_){}
      try{ await asRoot(['connection', 'up', 'uuid', prev.uuid], { timeout: 60000 }); }catch(_){}
    }
    const st = readState(); delete st[s.name]; writeState(st);
    return { ok: false, rolledBack: true, error: why + ' The bridge was removed and the previous connection restored.' };
  };
  try{
    await asRoot(['connection', 'add', 'type', 'bridge', 'ifname', s.name, 'con-name', s.name,
                  'bridge.stp', 'no', 'connection.autoconnect', 'yes'].concat(mac ? ['bridge.mac-address', mac] : [])
                  .concat(ip, ['ipv6.method', 'auto']), { timeout: 30000 });
    made.push(s.name);
    await asRoot(['connection', 'add', 'type', 'ethernet', 'slave-type', 'bridge', 'master', s.name,
                  'ifname', s.nic, 'con-name', port, 'connection.autoconnect', 'yes'], { timeout: 30000 });
    made.push(port);
    if(prev){
      const st = readState(); st[s.name] = { nic: s.nic, previous: prev.uuid, previousName: prev.name }; writeState(st);
      await asRoot(['connection', 'modify', 'uuid', prev.uuid, 'connection.autoconnect', 'no'], { timeout: 20000 });
    }
    await asRoot(['connection', 'up', 'id', port], { timeout: 60000 });
    await asRoot(['connection', 'up', 'id', s.name], { timeout: 60000 }).catch(() => {});
  }catch(e){
    return rollback('Creating the bridge failed: ' + String(e && e.message || e) + '.');
  }
  if(!await waitForAddress(s.name, BRIDGE_WAIT_MS))
    return rollback('The bridge did not get a network address within ' + Math.round(BRIDGE_WAIT_MS / 1000) + ' seconds.');
  const warnings = [];
  if(s.allowVms){
    try{
      if(!vmAllowed(s.name)) await exec(SUDO, ['-n', 'tee', '-a', bridgeConf()], { stdin: 'allow ' + s.name + '\n', timeout: 20000 });
      if(!helperSetuid()) await exec(SUDO, ['-n', 'chmod', 'u+s', bridgeHelper()], { timeout: 20000 });
    }catch(e){ warnings.push('The bridge works, but this computer\'s VMs could not be allowed onto it: ' + String(e && e.message || e)); }
  }
  return { ok: true, name: s.name, ipv4: await ipv4Of(s.name), vmReady: vmAllowed(s.name) && helperSetuid(), warnings };
}

async function deleteBridge(name){
  name = String(name || '').trim();
  if(!BRIDGE_RE.test(name)) return { ok: false, error: 'That is not a bridge name.' };
  const list = await bridges();
  const b = (list.bridges || []).find(x => x.name === name);
  if(!b) return { ok: false, error: 'There is no bridge called ' + name + '.' };
  if(b.managed !== 'nm') return { ok: false, error: name + ' belongs to ' + (b.managed === 'libvirt' ? 'libvirt (its NAT network)' : 'another program') + ' and is not removed here.' };
  const conns = await connections();
  const ms = await masters(conns.filter(c => c.type !== 'bridge').map(c => c.uuid)).catch(() => ({}));
  const ports = conns.filter(c => { const m = ms[c.uuid]; return m && m.type === 'bridge' && (m.master === b.uuid || m.master === name); });
  const st = readState();
  const rec = st[name];
  const errors = [];
  for(const p of ports){ try{ await asRoot(['connection', 'delete', 'uuid', p.uuid], { timeout: 20000 }); }catch(e){ errors.push(String(e.message || e)); } }
  try{ await asRoot(['connection', 'delete', 'uuid', b.uuid], { timeout: 20000 }); }
  catch(e){ return { ok: false, error: 'Could not remove the bridge: ' + String(e.message || e) }; }
  let restored = '';
  if(rec && rec.previous && conns.some(c => c.uuid === rec.previous)){
    try{
      await asRoot(['connection', 'modify', 'uuid', rec.previous, 'connection.autoconnect', 'yes'], { timeout: 20000 });
      await asRoot(['connection', 'up', 'uuid', rec.previous], { timeout: 60000 });
      restored = rec.previousName || rec.previous;
    }catch(e){ errors.push('the previous connection did not come back up: ' + String(e.message || e)); }
  }
  delete st[name]; writeState(st);
  return { ok: true, name, restored, warnings: errors };
}

module.exports = { available, devices, wifi, saved, connect, disconnect, forget, radio, status,
                   monitor, fields, bridges, createBridge, deleteBridge, cleanSpec };
