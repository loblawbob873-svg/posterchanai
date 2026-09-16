/* Runs the SHIPPED static/js/client/vms.js against a minimal DOM and a scripted host, and checks the
 * four rules its header promises — by what lands in #feed, in order:
 *   warm      a cached host + VM list is painted SYNCHRONOUSLY inside render(), before any network
 *             (the rpc stub never answers), phone and desktop widths
 *   cold      nothing cached → a spinner, never "No VM hosts yet" while the doc read is still out
 *   noAnswer  silence renders "No answer…" and KEEPS the last known VMs; it never says "no VMs"
 *   empty     an ANSWERED empty list says "No VMs are assigned to you" — distinct from silence
 *   ready     Relay.ready() is awaited before pcai:vmhosts is queried
 *   wipe      a doc read no relay answered (complete=false) must not be followed by a publish
 */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const SRC = fs.readFileSync(new URL('../../static/js/client/vms.js', import.meta.url), 'utf8');
const ME = 'ab'.repeat(32), HOST = 'cd'.repeat(32);
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));

function boot({ width = 390, cache = null, rpc, instance = null, relay = {}, pc = {} } = {}){
  const store = new Map();
  if(cache) store.set('pc_vms:' + ME, JSON.stringify(cache));
  const feed = { _html: '', paints: [], scrollTop: 0,
    set innerHTML(v){ this._html = v; this.paints.push(v); }, get innerHTML(){ return this._html; },
    getBoundingClientRect: () => ({ width }), querySelector: () => null, querySelectorAll: () => [] };
  const published = [];
  const log = [];
  const g = {
    console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, Date, JSON, Math, Promise,
    innerWidth: width,
    localStorage: { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)) },
    document: { hidden: false, head: { appendChild(){} }, documentElement: {}, body: { appendChild(){} },
      querySelector: s => s === '#feed' ? feed : null, getElementById: () => null,
      createElement: () => ({ style: {}, set textContent(v){} }) },
    PCVmRpc: { createVmRpc: () => ({ call: (h, op, a, o) => { log.push(op); return rpc(h, op, a, o); }, closeAll(){} }) },
    Relay: Object.assign({ ready: async () => { log.push('ready'); return true; } }, relay),
  };
  g.window = g;
  g.__PC = Object.assign({
    me: () => ({ pubkey: ME }), isView: v => v === 'vms', toast: m => log.push('toast:' + m),
    clientConfig: () => (instance ? { vmhost: instance } : {}),
    relayQuery: async () => { log.push('query'); const r = []; Object.defineProperty(r, 'complete', { value: true }); return r; },
    nip44dec: async () => '{}', nip44enc: async () => 'ct',
    publish: async (...a) => { published.push(a); return { ok: true }; },
    uiPrompt: async () => null, uiConfirm: async () => false,
  }, pc);
  vm.createContext(g);
  vm.runInContext(SRC, g);
  return { g, feed, log, published, store };
}

const never = () => new Promise(() => {});
const CACHE = {
  v: 1,
  hosts: [{ pubkey: HOST, relay: 'wss://host.example/relay', name: 'Cached host', source: 'added' }],
  data: { [HOST]: { at: Date.now() - 30000, whoami: { role: 'user', host: { name: 'Cached host' } },
                    info: { vms: { running: 1, total: 1 } },
                    vms: [{ uuid: '11111111-1111-4111-8111-111111111111', name: 'cached-vm', state: 'running', vcpus: 2, ram_mib: 2048, disk_gib: 20, assigned: [ME] }] } },
};

// ---- warm: painted synchronously, before any answer -------------------------------------------------
for (const width of [390, 1280]){
  const t = boot({ width, cache: CACHE, rpc: never, relay: { ready: never } });
  t.g.PCVms.render();
  assert.ok(t.feed.paints.length >= 1, 'render() must paint synchronously from the cache');
  const first = t.feed.paints[0];
  assert.match(first, /Cached host/, 'the cached host is on the FIRST paint (' + width + ')');
  assert.match(first, /Last answered 30s ago/);
  if (width >= 900) assert.match(first, /cached-vm/, 'desktop shows the cached VM list at once');
  assert.doesNotMatch(first, /No VM hosts yet|No VMs are assigned|No virtual machines/);
}
console.log('ok warm');

// ---- cold: a spinner, never an empty answer ------------------------------------------------------
{
  const t = boot({ width: 390, rpc: never, relay: { ready: never } });
  t.g.PCVms.render();
  await tick(5);
  assert.match(t.feed.innerHTML, /spinner/);
  assert.doesNotMatch(t.feed.innerHTML, /No VM hosts yet/, 'a doc read still out is not "no hosts"');
  assert.ok(!t.log.includes('query'), 'pcai:vmhosts must not be queried before Relay.ready() resolves');
}
console.log('ok cold + ready gate');

// ---- ready is awaited, then the doc is queried ---------------------------------------------------
{
  let release;
  const gate = new Promise(r => { release = r; });
  const t = boot({ width: 390, rpc: never, relay: { ready: async () => { t.log.push('ready'); await gate; return true; } } });
  t.g.PCVms.render();
  await tick(5);
  assert.deepEqual(t.log.filter(x => x === 'ready' || x === 'query'), ['ready']);
  release(); await tick(5);
  assert.deepEqual(t.log.filter(x => x === 'ready' || x === 'query'), ['ready', 'query']);
  await tick(5);
  assert.match(t.feed.innerHTML, /No VM hosts yet/, 'after an ANSWERED read with nothing in it, say so');
}
console.log('ok ready then query');

// ---- noAnswer keeps the stale list and says so ----------------------------------------------------
{
  const t = boot({ width: 1280, cache: CACHE, rpc: async () => ({ ok: false, noAnswer: true, reason: 'timeout' }) });
  t.g.PCVms.render();
  await tick(20);
  const html = t.feed.innerHTML;
  assert.match(html, /No answer — the host is offline or you’re not on its list/);
  assert.match(html, /cached-vm/, 'the last known list stays on screen');
  assert.doesNotMatch(html, /No VMs are assigned|No virtual machines on this host/);
}
{
  const inst = { pubkey: HOST, relay: 'wss://host.example/relay', name: 'This server' };
  const t = boot({ width: 1280, instance: inst, rpc: async () => ({ ok: false, noAnswer: true, reason: 'timeout' }) });
  t.g.PCVms.render();
  await tick(20);
  assert.match(t.feed.innerHTML, /No answer/);
  assert.doesNotMatch(t.feed.innerHTML, /No VMs are assigned|No virtual machines on this host/,
    'silence from a host with NOTHING cached must not read as an empty host');
}
console.log('ok noAnswer');

// ---- an answered empty list is a different sentence ----------------------------------------------
{
  const inst = { pubkey: HOST, relay: 'wss://host.example/relay', name: 'This server' };
  const answers = { 'host.whoami': { role: 'user', host: { name: 'This server' } }, 'host.info': { vms: { running: 0, total: 0 } },
                    'vm.list': { vms: [], next: null } };
  const t = boot({ width: 1280, instance: inst, rpc: async (h, op) => ({ ok: true, result: answers[op] }) });
  t.g.PCVms.render();
  await tick(20);
  assert.match(t.feed.innerHTML, /No VMs are assigned to you on this host/);
  assert.doesNotMatch(t.feed.innerHTML, /No answer/);
  const saved = JSON.parse(t.store.get('pc_vms:' + ME));
  assert.equal(saved.hosts[0].pubkey, HOST, 'the answer is cached for the next cold open');
}
console.log('ok empty answered');

// ---- an unanswered doc read must not be written over ----------------------------------------------
{
  const prompts = ['npub-or-hex', 'wss://new.example/relay'];
  const t = boot({ width: 390, rpc: never, pc: {
    relayQuery: async () => { const r = []; Object.defineProperty(r, 'complete', { value: false }); return r; },
    uiPrompt: async () => prompts.shift() === 'npub-or-hex' ? 'ef'.repeat(32) : 'wss://new.example/relay',
  } });
  t.g.PCVms.render();
  await tick(10);
  await t.g.PCVms._addHost();
  await tick(10);
  assert.equal(t.published.length, 0, 'no relay answered the read, so the host list must not be published');
  assert.ok(t.log.some(x => /not saved there/.test(x)), 'and the person is told it was kept on this device');
  assert.ok(t.g.PCVms._state.hosts.some(h => h.pubkey === 'ef'.repeat(32)), 'the host is still added locally');
}
{
  const t = boot({ width: 390, rpc: never, pc: {
    uiPrompt: (() => { const a = ['ef'.repeat(32), 'wss://new.example/relay']; return async () => a.shift(); })(),
  } });
  t.g.PCVms.render();
  await tick(10);
  await t.g.PCVms._addHost();
  await tick(10);
  assert.equal(t.published.length, 1, 'after an ANSWERED read the list is saved');
  assert.equal(t.published[0][0], 30078);
  assert.equal(JSON.stringify(t.published[0][2]), JSON.stringify([['d', 'pcai:vmhosts']]));
}
console.log('ok doc wipe guard');
console.log('ALL OK');
