/* Runs the SHIPPED static/js/client/vms.js (phase 2 paths) against a minimal DOM, a scripted host and a
 * `window.pcVM` stub BUILT FROM desktop/preload.js — the method list is parsed out of the real
 * `exposeInMainWorld('pcVM', {...})`, and the stub throws on any other name, so the adapter cannot call a
 * method the desktop app does not actually expose.
 *
 *   local     "This computer" is the first host when pcVM exists, is never written into pcai:vmhosts,
 *             and every client op maps onto the preload API with the right arguments
 *   sessions  a REMOTE signer polls through a session key (session.open once, use ops signed by the
 *             session), management ops go out under the real key, an ended session is dropped and
 *             reopened; a LOCAL nsec never opens a session
 *   find      Find hosts adds exactly the announced hosts that ANSWER host.whoami, and saves them
 */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const SRC = fs.readFileSync(new URL('../../static/js/client/vms.js', import.meta.url), 'utf8');
const NOSTR = fs.readFileSync(new URL('../../static/vendor/nostr/nostr.bundle.js', import.meta.url), 'utf8');
const PRELOAD = fs.readFileSync(new URL('../../desktop/preload.js', import.meta.url), 'utf8');
const ME = 'ab'.repeat(32), HOST = 'cd'.repeat(32);
const eq = (a, b, m) => assert.deepEqual(JSON.parse(JSON.stringify(a === undefined ? null : a)), JSON.parse(JSON.stringify(b === undefined ? null : b)), m);
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));

function preloadVmMethods(){
  const i = PRELOAD.indexOf("exposeInMainWorld('pcVM'");
  assert.ok(i > 0, 'preload.js no longer exposes pcVM');
  const body = PRELOAD.slice(i, PRELOAD.indexOf('});', i));
  const names = [...body.matchAll(/^\s*([A-Za-z]+)\s*:/gm)].map(m => m[1]);
  assert.ok(names.length >= 12, 'parsed too few pcVM methods: ' + names);
  return names;
}

function pcVMStub(log, impl){
  const names = preloadVmMethods();
  const obj = {};
  for(const n of names) obj[n] = async (...a) => { log.push([n, ...a]); return impl[n] ? impl[n](...a) : { ok: true }; };
  return new Proxy(obj, { get(t, k){ if(typeof k === 'string' && !(k in t) && k !== 'then') throw new Error('pcVM has no method ' + k); return t[k]; } });
}

function boot({ width = 1280, rpc, pcVM = null, mode = 'local', pc = {}, preset = {} } = {}){
  const store = new Map(Object.entries(preset));
  const feed = { _html: '', paints: [], scrollTop: 0,
    set innerHTML(v){ this._html = v; this.paints.push(v); }, get innerHTML(){ return this._html; },
    getBoundingClientRect: () => ({ width }), querySelector: () => null, querySelectorAll: () => [] };
  const published = [];
  const log = [];
  const calls = [];
  const g = {
    console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, Date, JSON, Math, Promise,
    Uint8Array, TextEncoder, TextDecoder, crypto: globalThis.crypto, innerWidth: width,
    localStorage: { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)),
                    removeItem: k => store.delete(k), key: i => [...store.keys()][i] ?? null, get length(){ return store.size; } },
    document: { hidden: false, head: { appendChild(){} }, documentElement: {}, body: { appendChild(){} },
      querySelector: s => s === '#feed' ? feed : null, getElementById: () => null,
      createElement: () => ({ style: {}, set textContent(v){} }) },
    PCVmRpc: { createVmRpc: () => ({ call: (h, op, a, o) => { calls.push({ host: h.pubkey, op, a, o: o || {} }); return rpc(h, op, a, o || {}); }, closeAll(){} }) },
    Relay: { ready: async () => true },
  };
  g.window = g;
  if(pcVM) g.pcVM = pcVM;
  vm.createContext(g);
  vm.runInContext(NOSTR + '\nglobalThis.NostrTools=NostrTools;', g);
  g.__PC = Object.assign({
    me: () => ({ pubkey: ME, mode }), isView: v => v === 'vms', toast: m => log.push('toast:' + m),
    clientConfig: () => ({}),
    relayQuery: async () => { const r = []; Object.defineProperty(r, 'complete', { value: true }); return r; },
    nip44dec: async () => '{}', nip44enc: async (pk, t) => t,
    publish: async (...a) => { published.push(a); return { ok: true }; },
    uiPrompt: async () => null, uiConfirm: async () => false,
    signTemplate: async t => t,
  }, pc);
  vm.runInContext(SRC, g);
  return { g, feed, log, published, store, calls };
}
const never = () => new Promise(() => {});

// ---- local host ---------------------------------------------------------------------------------------
{
  const vlog = [];
  const details = { ok: true, name: 'win11', state: 'shut off', ramMiB: 4096, cpus: 4, autostart: false, bootOrder: 'cdrom',
                    gamingMouse: false, networks: 1, disks: [{ type: 'file', device: 'disk', target: 'vda', source: '/h/disk.qcow2' },
                                                             { type: 'file', device: 'cdrom', target: 'sda', source: '/h/isos/Win11.iso' }] };
  const stub = pcVMStub(vlog, {
    list: () => ({ available: true, machines: [{ name: 'win11', state: 'running', memoryKiB: 4194304, cpus: 4, autostart: false, missingMedia: [] },
                                               { name: 'deb', state: 'shut off', memoryKiB: 2097152, cpus: 2, missingMedia: ['/gone.iso'] }] }),
    details: () => details, create: () => ({ ok: true, name: 'newvm' }), pickIso: () => '/home/u/alpine.iso',
  });
  const prompts = ['ef'.repeat(32), 'wss://new.example/relay'];
  const t = boot({ pcVM: stub, rpc: never, pc: { uiPrompt: async () => prompts.shift() } });
  t.g.PCVms.render();
  const S = t.g.PCVms._state;
  assert.equal(S.hosts[0].pubkey, 'local', '"This computer" is listed FIRST');
  await tick(20);
  assert.match(t.feed.innerHTML, /This computer/);
  assert.match(t.feed.innerHTML, /win11/, 'the local VM list is painted');
  assert.equal(t.calls.filter(c => c.host === 'local').length, 0, 'the local host never goes to the Nostr rpc');

  const C = t.g.PCVms._call;
  const list = await C('local', 'vm.list');
  eq(list.result.vms.map(v => [v.uuid, v.state, v.ram_mib, v.missing_media]),
                   [['win11', 'running', 4096, ''], ['deb', 'shutoff', 2048, '/gone.iso']]);
  const who = await C('local', 'host.whoami');
  assert.equal(who.result.role, 'admin');
  eq({ ...who.result.host.features }, { assign: false, migrate: false, snapshots: false, console: 'spice', iso: 'picker',
                                              hardware: true, access: false, local: true });
  const get = await C('local', 'vm.get', { vm: 'win11' });
  eq({ ...get.result.vm.hardware, disks: undefined },
                   { boot: 'cdrom', input: 'tablet', nics: 1, disks: undefined, media: 'Win11.iso', media_path: '/h/isos/Win11.iso', cdrom: true });

  vlog.length = 0;
  assert.ok((await C('local', 'vm.power', { vm: 'win11', action: 'destroy' })).ok);
  eq(vlog[0], ['action', 'win11', 'stop'], 'force off is desktop/vm.js "stop"');
  vlog.length = 0;
  await C('local', 'vm.power', { vm: 'deb', action: 'start' });
  eq(vlog.slice(0, 2), [['action', 'deb', 'start'], ['view', 'deb']], 'starting a local VM opens its display');

  vlog.length = 0;
  const up = await C('local', 'vm.update', { vm: 'win11', vcpus: 6, boot: 'disk', add_disk_gib: 10, add_nic: true,
                                             media: { path: '/home/u/alpine.iso' }, input: 'mouse' });
  assert.ok(up.ok, JSON.stringify(up));
  eq(vlog.map(x => x[0]), ['details', 'update', 'addDisk', 'addNetwork', 'changeIso', 'gamingMouse', 'details']);
  eq(vlog[1], ['update', 'win11', { ramMiB: 4096, cpus: 6, autostart: false, bootOrder: 'disk' }]);
  eq(vlog[4], ['changeIso', 'win11', '/home/u/alpine.iso']);
  eq(vlog[5], ['gamingMouse', 'win11', true]);
  vlog.length = 0;
  await C('local', 'vm.update', { vm: 'win11', media: 'eject' });
  eq(vlog.map(x => x[0]), ['details', 'ejectIso', 'details']);

  vlog.length = 0;
  const fail = pcVMStub(vlog, { details: () => details, addDisk: () => ({ ok: false, error: 'No virtual disk slots are available' }) });
  t.g.pcVM = fail;
  const bad = await C('local', 'vm.update', { vm: 'win11', add_disk_gib: 5, add_nic: true });
  assert.equal(bad.ok, false);
  assert.match(bad.error.message, /No virtual disk slots/);
  assert.ok(!vlog.some(x => x[0] === 'addNetwork'), 'the first failure stops the rest');
  t.g.pcVM = stub;

  vlog.length = 0;
  assert.ok((await C('local', 'console.open', { vm: 'win11' })).ok);
  eq(vlog, [['view', 'win11']], 'the local console is SPICE via pcVM.view');
  vlog.length = 0;
  const made = await C('local', 'vm.create', { name: 'newvm', iso: '/home/u/alpine.iso', guest: 'linux', firmware: 'efi', vcpus: 2, ram_mib: 2048, disk_gib: 20 });
  assert.ok(made.ok);
  eq(vlog[0], ['create', { name: 'newvm', iso: '/home/u/alpine.iso', guest: 'linux', firmware: 'efi', ramMiB: 2048, cpus: 2, diskGiB: 20, network: { type: 'user' } }], 'no network chosen = user-mode NAT');
  assert.equal((await C('local', 'vm.create', { name: 'x' })).error.code, 'bad_request', 'a local create needs a picked ISO');
  vlog.length = 0;
  assert.equal((await C('local', 'iso.pick')).result.path, '/home/u/alpine.iso');
  assert.ok((await C('local', 'vm.boot_disk', { vm: 'deb' })).ok);
  eq(vlog.slice(1).map(x => x[0]), ['bootDisk', 'action', 'view']);
  assert.ok((await C('local', 'vm.delete', { vm: 'deb', delete_disks: true })).ok);
  eq(vlog[vlog.length - 1], ['remove', 'deb', true]);
  assert.equal((await C('local', 'vm.snapshot.create', { vm: 'deb', name: 'x' })).error.code, 'unsupported');

  await t.g.PCVms._addHost();
  await tick(10);
  const doc = JSON.parse(t.published[t.published.length - 1][1]);
  eq(doc.hosts.map(h => h.pubkey), ['ef'.repeat(32)], 'the local host is never written into pcai:vmhosts');

  const noVm = boot({ rpc: never });
  noVm.g.PCVms.render();
  assert.ok(!noVm.g.PCVms._state.hosts.some(h => h.pubkey === 'local'), 'no pcVM → no "This computer"');
}
console.log('ok local host');

// ---- session keys -------------------------------------------------------------------------------------
function hostCache(){
  return { v: 1, hosts: [{ pubkey: HOST, relay: 'wss://host.example/relay', name: 'H', source: 'added' }], data: {} };
}
{
  const opened = [];
  let expireNext = false;
  const rpc = async (h, op, a, o) => {
    if(op === 'session.open'){ opened.push(a); return { ok: true, result: { pk: a.pk, exp: a.exp } }; }
    if(o.signer && expireNext){ expireNext = false; return { ok: false, error: { code: 'session_expired', message: 'ended' } }; }
    return { ok: true, result: op === 'vm.list' ? { vms: [] } : op === 'host.whoami' ? { role: 'admin', host: { features: ['hardware'] } } : {} };
  };
  const t = boot({ rpc, mode: 'nip46' });
  t.store.set('pc_vms:' + ME, JSON.stringify(hostCache()));
  t.g.PCVms.render();
  await tick(30);
  const C = t.g.PCVms._call;
  t.calls.length = 0;
  await C(HOST, 'vm.list', {});
  await C(HOST, 'vm.power', { vm: 'x', action: 'start' });
  assert.equal(opened.length, 1, 'ONE session per host, reused');
  const NT = t.g.NostrTools;
  const pr = opened[0].proof;
  assert.ok(NT.verifyEvent(pr) && pr.pubkey === opened[0].pk && pr.kind === 27310);
  assert.equal(pr.content, 'posterchan-vmhost-session:' + ME + ':' + opened[0].exp + ':' + HOST);
  const spk = opened[0].pk;
  assert.ok(t.calls.every(c => c.o.signer && c.o.signer.pubkey === spk), 'use ops are signed by the session key');
  t.calls.length = 0;
  await C(HOST, 'vm.update', { vm: 'x', vcpus: 2 });
  await C(HOST, 'iso.list', {});
  await C(HOST, 'host.access.set', { allowed: [] });
  assert.ok(t.calls.every(c => !c.o.signer), 'management ops go out under the REAL key');
  // The session SECRET is never written to localStorage (readable by any script on the origin, and kept across
  // sign-outs): it lives in memory for this page and this account only.
  assert.ok(![...t.store.keys()].some(k => k.startsWith('pc_vms_sess:')), 'a session secret was persisted to localStorage');
  assert.ok(![...t.store.values()].some(v => String(v).includes(opened[0].pk) || /"sk":"[0-9a-f]{64}"/.test(String(v))),
            'no stored value carries the session key');
  // An ended session: dropped, the call falls back to the real key, and the next use op reopens.
  expireNext = true;
  t.calls.length = 0;
  const r = await C(HOST, 'vm.list', {});
  assert.ok(r.ok);
  eq(t.calls.map(c => !!c.o.signer), [true, false], 'session_expired → retried with the real key');
  await C(HOST, 'vm.get', { vm: 'x' });
  assert.equal(opened.length, 2, 'the next use op opens a fresh session');
  assert.notEqual(opened[1].pk, spk);
  await C(HOST, 'vm.get', { vm: 'x' });
  assert.equal(opened.length, 2, 'and reuses it');
  // Sign-out / account switch drops every session secret of the account that left.
  t.g.__PC.me = () => null;
  t.g.PCVms.render();
  t.g.__PC.me = () => ({ pubkey: ME, mode: 'nip46' });
  t.g.PCVms.render();
  await tick(30);
  await C(HOST, 'vm.get', { vm: 'x' });
  assert.equal(opened.length, 3, 'after a sign-out the old session key is gone: a new one is opened');
}
{
  // A session secret an OLDER build left in localStorage is removed when the screen loads.
  const t = boot({ mode: 'nip46', rpc: async () => ({ ok: true, result: {} }),
                   preset: { ['pc_vms_sess:' + ME]: JSON.stringify({ [HOST]: { sk: 'ee'.repeat(32), exp: 9e9 } }),
                             ['pc_vms_sess:' + 'ff'.repeat(32)]: '{}', ['pc_vms:' + ME]: JSON.stringify(hostCache()) } });
  t.g.PCVms.render();
  await tick(30);
  assert.ok(![...t.store.keys()].some(k => k.startsWith('pc_vms_sess:')), 'legacy session secrets were purged');
  assert.ok(t.store.has('pc_vms:' + ME), 'the host cache is not a secret and stays');
}
{
  const t = boot({ mode: 'local', rpc: async (h, op) => ({ ok: true, result: op === 'vm.list' ? { vms: [] } : {} }) });
  t.store.set('pc_vms:' + ME, JSON.stringify(hostCache()));
  t.g.PCVms.render();
  await tick(30);
  await t.g.PCVms._call(HOST, 'vm.list', {});
  assert.ok(!t.calls.some(c => c.op === 'session.open' || c.o.signer), 'a local nsec never uses a session');
}
{
  // A host without sessions answers session.open with an error: stop asking, use the real key.
  let opens = 0;
  const t = boot({ mode: 'nip07', rpc: async (h, op) => op === 'session.open' ? (opens++, { ok: false, error: { code: 'unsupported', message: 'unknown operation' } })
                                                                            : { ok: true, result: { vms: [] } } });
  t.store.set('pc_vms:' + ME, JSON.stringify(hostCache()));
  t.g.PCVms.render();
  await tick(30);
  for(let i = 0; i < 4; i++) assert.ok((await t.g.PCVms._call(HOST, 'vm.list', {})).ok);
  assert.equal(opens, 1, 'a refused session.open is not retried on every poll');
}
console.log('ok sessions');

// ---- find hosts ------------------------------------------------------------------------------------------
{
  const A = 'a1'.repeat(32), B = 'b2'.repeat(32), KNOWN = HOST;
  const now = Math.floor(Date.now() / 1000);
  const ann = (pk, relay, name, ts = now) => ({ kind: 31310, pubkey: pk, created_at: ts, tags: [['d', 'posterchan-vmhost'], ['relay', relay]],
                                                 content: JSON.stringify({ v: 1, name, relays: [relay], https: '' }) });
  const events = [ann(A, 'wss://a.example/relay', 'Alpha host'), ann(B, 'wss://b.example/relay', 'Beta host'),
                  ann(A, 'wss://old.example/relay', 'Alpha old', now - 999), ann(KNOWN, 'wss://host.example/relay', 'known'),
                  ann(B, 'https://not-a-relay', 'bad', now - 5)];
  let queried = null;
  const t = boot({ mode: 'local', rpc: async (h, op) => {
      if(op !== 'host.whoami') return { ok: true, result: { vms: [] } };
      return h.pubkey === A ? { ok: true, result: { role: 'user', host: { name: 'Alpha host', features: [] } } } : { ok: false, noAnswer: true };
    }, pc: { relayQuery: async f => { queried = f; const r = events.slice(); Object.defineProperty(r, 'complete', { value: true }); return r; } } });
  t.store.set('pc_vms:' + ME, JSON.stringify(hostCache()));
  t.g.PCVms.render();
  await tick(20);
  t.calls.length = 0;
  await t.g.PCVms._findHosts();
  await tick(10);
  eq(JSON.parse(JSON.stringify(queried)), [{ kinds: [31310], '#d': ['posterchan-vmhost'], limit: 100 }]);
  const asked = [...new Set(t.calls.filter(c => c.op === 'host.whoami').map(c => c.host))].sort();
  eq(asked, [A, B].sort(), 'every NEW announced host is asked; a known one is not');
  const S = t.g.PCVms._state;
  assert.ok(S.hosts.some(h => h.pubkey === A && h.relay === 'wss://a.example/relay'), 'the newest announcement’s relay is used');
  assert.ok(!S.hosts.some(h => h.pubkey === B), 'a host that does not answer is NOT added');
  assert.equal(S.find.silent, 1);
  const doc = JSON.parse(t.published[t.published.length - 1][1]);
  assert.ok(doc.hosts.some(h => h.pubkey === A), 'found hosts are saved to pcai:vmhosts');
  assert.match(t.feed.innerHTML, /Alpha host/);
}
console.log('ok find hosts');
console.log('ALL OK');
