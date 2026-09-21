/* The VM network choice in the SHIPPED vms.js: which networks are offered, what Create and Settings send,
 * and that "This computer" hands a bridge choice to window.pcVM (create / setNetwork) exactly.
 *   remote   host.info networks+bridges → options; default = the host's bridge, else its NAT network;
 *            a host that lists none offers no picker at all (an older host would refuse the field)
 *   delta    Settings sends `network` only when the choice differs from the VM's current NIC
 *   local    NAT (user) is always offered; a bridge that is not VM-ready is listed but disabled;
 *            vm.create passes {type:'bridge',name} to pcVM.create, vm.update calls pcVM.setNetwork
 */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const SRC = fs.readFileSync(new URL('../../static/js/client/vms.js', import.meta.url), 'utf8');
const HOST = 'cd'.repeat(32);
const J = x => JSON.parse(JSON.stringify(x === undefined ? null : x));

const calls = [];
const g = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, Date, JSON, Math, Promise,
  innerWidth: 1280, localStorage: { getItem: () => null, setItem(){}, removeItem(){}, key: () => null, length: 0 },
  document: { hidden: false, head: { appendChild(){} }, documentElement: {}, body: { appendChild(){} },
    querySelector: () => null, getElementById: () => null, createElement: () => ({ style: {} }) },
  pcVM: {
    list: async () => ({ available: true, machines: [] }),
    create: async (o) => { calls.push(['create', o]); return { ok: true, name: o.name }; },
    view: async () => ({ ok: true }),
    details: async (n) => ({ ok: true, name: n, state: 'shut off', ramMiB: 2048, cpus: 2, disks: [], nic: { type: 'bridge', source: 'br0' } }),
    setNetwork: async (n, net) => { calls.push(['setNetwork', n, net]); return { ok: true }; },
  },
};
g.window = g;
vm.createContext(g);
vm.runInContext(SRC, g);
const V = g.PCVms, N = V._net;

// ---- remote
V._state.data[HOST] = { info: { networks: ['default', 'isolated'], bridges: ['br0'], default_network: 'default', bridge: '' } };
assert.deepEqual(J(N.netChoices(HOST).map(c => c.v)), ['network:default', 'network:isolated', 'bridge:br0']);
assert.equal(N.netDefault(HOST), 'network:default');
V._state.data[HOST].info.bridge = 'br0';
assert.equal(N.netDefault(HOST), 'bridge:br0', "a host configured with a bridge defaults to it");
V._state.data[HOST] = { info: {} };
assert.equal(N.netChoices(HOST).length, 0, 'a host that lists nothing gets no picker');
assert.deepEqual(J(N.parseNet('bridge:br0')), { type: 'bridge', name: 'br0' });
assert.deepEqual(J(N.parseNet('user:')), { type: 'user' });
assert.equal(N.parseNet(''), null);
assert.equal(N.netValue({ type: 'network', name: 'default' }), 'network:default');
console.log('ok remote');

// ---- delta: only a CHANGE is sent
V._state.settings = { pk: HOST, vm: { vcpus: 2, ram_mib: 2048, autostart: false }, hw: { net: { type: 'network', name: 'default' } } };
const base = { vcpus: 2, ram_mib: 2048, autostart: false, media: '__keep', add_disk_gib: 0, add_nic: false, pick: '' };
assert.equal(V._settingsDelta(Object.assign({}, base, { net: 'network:default' })).network, undefined);
assert.equal(V._settingsDelta(Object.assign({}, base, { net: '' })).network, undefined, '"keep" sends nothing');
assert.deepEqual(J(V._settingsDelta(Object.assign({}, base, { net: 'bridge:br0' })).network), { type: 'bridge', name: 'br0' });
console.log('ok delta');

// ---- local
const lc = N.netChoices('local', [{ name: 'br0', vmReady: true }, { name: 'br1', vmReady: false }]);
assert.deepEqual(J(lc.map(c => [c.v, !!c.off])), [['user:', false], ['bridge:br0', false], ['bridge:br1', true]]);
let r = await V._local.call('vm.create', { name: 'x', iso: '/tmp/a.iso', network: { type: 'bridge', name: 'br0' } });
assert.ok(r.ok, JSON.stringify(r));
assert.deepEqual(J(calls.find(c => c[0] === 'create')[1].network), { type: 'bridge', name: 'br0' });
calls.length = 0;
r = await V._local.call('vm.create', { name: 'y', iso: '/tmp/a.iso' });
assert.deepEqual(J(calls.find(c => c[0] === 'create')[1].network), { type: 'user' }, 'no choice = user-mode NAT, as before');
r = await V._local.call('vm.update', { vm: 'x', network: { type: 'bridge', name: 'br0' } });
assert.ok(r.ok, JSON.stringify(r));
assert.deepEqual(J(calls.find(c => c[0] === 'setNetwork')), ['setNetwork', 'x', { type: 'bridge', name: 'br0' }]);
assert.deepEqual(J(r.result.vm.hardware.net), { type: 'bridge', name: 'br0' }, 'the local NIC is read back into hardware.net');
console.log('ok local');
console.log('ALL OK');
