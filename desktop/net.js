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

const NMCLI = process.env.PC_NMCLI || 'nmcli';

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

function run(args, opts){
  const o = opts || {};
  return new Promise((resolve, reject) => {
    const child = execFile(NMCLI, args, { timeout: o.timeout || 45000, maxBuffer: 4 << 20 },
      (err, stdout, stderr) => {
        if(err){
          /* nmcli says WHY on stderr and exits non-zero; the message is the only thing that can tell
           * "wrong password" from "no such network" from "the radio is off", and a caller handed a
           * bare exit code cannot tell a person anything useful. */
          const why = String(stderr || err.message || '').trim().split('\n').pop();
          const e = new Error(why || 'nmcli failed');
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
  return [...best.values()].sort((a, b) => (b.active - a.active) || (b.signal - a.signal));
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
  const args = ['device', 'wifi', 'connect', ssid];
  if(password) args.push('--ask');
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

module.exports = { available, devices, wifi, saved, connect, disconnect, forget, radio, status,
                   monitor, fields };
