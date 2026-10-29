// PosterChanOS pairs PosterFox's Passwords add-on: the shipped vault.js with a fake desktop bridge (pcShell).
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
const source = fs.readFileSync(new URL('../../static/js/client/vault.js', import.meta.url), 'utf8');
const core = fs.readFileSync(new URL('../../static/js/client/vaultcore.js', import.meta.url), 'utf8');
async function harness({ noKey = false, locked = false, signer = 'local' } = {}) {
  const data = new Map(), sent = [];
  const ctx = vm.createContext({ crypto: webcrypto, URL, Uint8Array, TextEncoder, TextDecoder, atob, btoa,
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    navigator: { onLine: true }, document: { querySelector: () => null, addEventListener: () => {} },
    localStorage: { getItem: k => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), removeItem: k => data.delete(k) } });
  ctx.window = ctx; ctx.addEventListener = () => {};
  vm.runInContext(core, ctx);
  const V = ctx.PCVaultCore, key = V.newVaultKey();
  if (!noKey) data.set('pcaiVaultKey:raw:alice', V.toB64(key));
  if (locked) data.set('pcaiVaultStayUnlocked', '0');
  const sess = signer === 'local' ? { mode: 'local', sk: 'cc'.repeat(32) }
             : signer === 'nip07' ? { mode: 'nip07' } : null;
  ctx.__PC = { ME: { pubkey: 'alice', mode: signer }, VIEW: 'home', isView: () => false, CFG: { relay_url: 'wss://node.test/relay' },
    apiBase: () => 'https://node.test', mediaServer: () => '', signerSession: () => sess,
    publish: () => { throw Error('pairing the browser must never create or publish a vault key'); },
    nip44dec: async () => JSON.stringify({ k: V.toB64(key) }) };
  ctx.Store = { query: () => [] };
  ctx.Relay = { ready: async () => true, urls: () => ['wss://node.test/relay'], query: async () => { const r = []; r.complete = true; return r; } };
  ctx.pcShell = { setBrowserPairing: async code => { sent.push(code); return code ? 'written' : 'removed'; } };
  vm.runInContext(source, ctx);
  return { ctx, sent, key, V };
}
const decode = c => JSON.parse(Buffer.from(c, 'base64').toString('utf8'));

let h = await harness();
assert.equal(await h.ctx.PCVault.syncOS(), true);
assert.equal(h.sent.length, 1, 'nothing was handed to the desktop');
let p = decode(h.sent[0]);
assert.equal(p.t, 'pcvault'); assert.equal(p.pubkey, 'alice'); assert.equal(p.mode, 'full');
assert.equal(p.key, h.V.toB64(h.key)); assert.equal(p.sk, 'cc'.repeat(32)); assert.deepEqual(p.relays, ['wss://node.test/relay']);
assert.equal(p.api, 'https://node.test');
await h.ctx.PCVault.syncOS();
assert.equal(h.sent.length, 1, 'an unchanged pairing was written again on every relay reconnect');
await h.ctx.PCVault.forget();
assert.equal(h.sent.at(-1), null, 'sign-out did not remove the browser pairing');
await h.ctx.PCVault.syncOS();
assert.equal(h.sent.at(-1), null, 'a retired vault paired the browser again');
console.log('PASS: full pairing handed over once; removed at sign-out');

h = await harness({ signer: 'nip07' });
await h.ctx.PCVault.syncOS();
assert.equal(decode(h.sent[0]).mode, 'ro', 'a signer that cannot delegate must pair read-only, not fail');
console.log('PASS: read-only when the signer cannot delegate');

h = await harness({ locked: true });
await h.ctx.PCVault.syncOS();
assert.deepEqual(h.sent, [null], '"stay unlocked" off must not hand the key to the browser');
h = await harness();
await h.ctx.PCVault.syncOS();
h.ctx.PCVault.setStayUnlocked(false);
assert.equal(h.sent.at(-1), null, 'turning "stay unlocked" off did not unpair the browser');
console.log('PASS: follows "stay unlocked"');

h = await harness({ noKey: true });
await h.ctx.PCVault.syncOS();
assert.equal(h.sent.length, 0, 'no vault yet: nothing to pair, and no vault minted');
console.log('PASS: never mints a vault');

h = await harness();
delete h.ctx.pcShell;
assert.equal(await h.ctx.PCVault.syncOS(), false, 'outside PosterChanOS there is no desktop to hand it to');
console.log('PASS: no-op outside PosterChanOS');
