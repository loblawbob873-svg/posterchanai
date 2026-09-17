/* Runs the SHIPPED static/js/client/vmrpc.js under node, against a stub relay socket, with REAL
 * NostrTools signing, verification and NIP-44 — the host side is simulated with its own key.
 *
 * What it proves (each scenario fails against a plausible wrong implementation):
 *   correlation   the REQ is on the wire BEFORE the EVENT; a result is only accepted when it is
 *                 authored by the host, e-tags THIS request, verifies, and carries THIS id — decoys
 *                 for each of those are delivered first and must be ignored; progress reaches onProgress
 *   noAnswer      silence is {ok:false, noAnswer:true}, never an ok with an empty result
 *   retry         a silent attempt is re-sent as a NEW event with the SAME idempotency id
 *   refused       a relay OK:false is an error, not a retry and not "no answer"
 *   hostError     the host's error code passes through untouched
 * Mode PC_VMRPC_INTEROP=1: emit one real request on stdout, read the Python host's reply on stdin,
 * print the decoded outcome — the wrapper uses it to cross-check the two implementations.
 */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import readline from 'node:readline';

globalThis.window = globalThis;
vm.runInThisContext(fs.readFileSync(new URL('../../static/vendor/nostr/nostr.bundle.js', import.meta.url), 'utf8') + '\nglobalThis.NostrTools=NostrTools;');
vm.runInThisContext(fs.readFileSync(new URL('../../static/js/client/vmrpc.js', import.meta.url), 'utf8'));
const NT = globalThis.NostrTools, R = globalThis.PCVmRpc;

const userSk = new Uint8Array(32).fill(7), hostSk = new Uint8Array(32).fill(9), otherSk = new Uint8Array(32).fill(11);
const USER = NT.getPublicKey(userSk);
const hostPkArg = process.env.PC_VMRPC_HOST || '';
const HOST = hostPkArg || NT.getPublicKey(hostSk);
const ck = (sk, pk) => NT.nip44.v2.utils.getConversationKey(sk, pk);
const enc = (sk, pk, t) => NT.nip44.v2.encrypt(t, ck(sk, pk));
const dec = (sk, pk, c) => NT.nip44.v2.decrypt(c, ck(sk, pk));

function deps(WS, extra){
  return Object.assign({
    WebSocket: WS, me: () => USER,
    sign: async t => NT.finalizeEvent(t, userSk),
    enc: async (pk, t) => enc(userSk, pk, t),
    dec: async (pk, c) => dec(userSk, pk, c),
    verify: ev => NT.verifyEvent(ev),
    timeout: 250, connectTimeout: 500, idleClose: 50,
  }, extra || {});
}

/* A relay socket whose far end is a script: `onSend(msg, sock)` sees every frame the client sends. */
function relayClass(onSend){
  return class Stub {
    constructor(url){ this.url = url; this.readyState = 0; this.sent = []; Stub.last = this;
      setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 1); }
    send(raw){ const m = JSON.parse(raw); this.sent.push(m); onSend(m, this); }
    deliver(m){ setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(m) }), 1); }
    close(){ if(this.readyState === 3) return; this.readyState = 3; this.onclose && this.onclose(); }
  };
}
const HOSTOBJ = { pubkey: HOST, relay: 'wss://host.example/relay' };

function hostReply(req, payload, kind = 6310, sk = hostSk, eid = req.id){
  return NT.finalizeEvent({ kind, created_at: Math.floor(Date.now() / 1000), content: enc(sk, USER, JSON.stringify(payload)),
                            tags: [['e', eid], ['p', USER], ['nofederate'], ['expiration', String(Math.floor(Date.now() / 1000) + 300)]] }, sk);
}

if (process.env.PC_VMRPC_INTEROP === '1') {
  const rl = readline.createInterface({ input: process.stdin });
  const lines = rl[Symbol.asyncIterator]();
  const WS = relayClass(async (m, sock) => {
    if (m[0] === 'REQ') sock.subId = m[1];
    if (m[0] === 'EVENT') {
      process.stdout.write('REQUEST ' + JSON.stringify(m[1]) + '\n');
      sock.deliver(['OK', m[1].id, true, '']);
      const { value } = await lines.next();
      sock.deliver(['EVENT', sock.subId, JSON.parse(value)]);
    }
  });
  const rpc = R.createVmRpc(deps(WS, { timeout: 8000 }));
  const out = await rpc.call(HOSTOBJ, 'vm.power', { vm: '11111111-1111-4111-8111-111111111111', action: 'start' }, { id: 'interop-1' });
  process.stdout.write('RESULT ' + JSON.stringify(out) + '\n');
  rpc.closeAll();
  process.exit(0);
}

// ---- correlation ------------------------------------------------------------------------------
{
  const order = [];
  const progress = [];
  const WS = relayClass((m, sock) => {
    order.push(m[0]);
    if (m[0] === 'REQ') sock.subId = m[1];
    if (m[0] !== 'EVENT') return;
    const req = m[1];
    assert.equal(req.kind, 5310);
    assert.ok(req.tags.some(t => t[0] === 'p' && t[1] === HOST), 'p-tag the host');
    assert.ok(req.tags.some(t => t[0] === 'nofederate'), 'nofederate');
    const exp = Number(req.tags.find(t => t[0] === 'expiration')[1]);
    assert.ok(exp > req.created_at && exp <= req.created_at + 120, 'short expiration');
    assert.ok(NT.verifyEvent(req));
    const body = JSON.parse(dec(hostSk, USER, req.content));
    assert.equal(body.v, 1); assert.equal(body.op, 'vm.list');
    sock.deliver(['OK', req.id, true, '']);
    // Decoys first — each wrong in exactly one way.
    sock.deliver(['EVENT', sock.subId, hostReply(req, { v: 1, id: body.id, ok: true, result: { decoy: 'wrong e' } }, 6310, hostSk, 'f'.repeat(64))]);
    // Wrong author: the HOST's ciphertext (so it decrypts), re-signed by somebody else.
    const stolen = hostReply(req, { v: 1, id: body.id, ok: true, result: { decoy: 'wrong author' } });
    sock.deliver(['EVENT', sock.subId, NT.finalizeEvent({ kind: 6310, created_at: stolen.created_at, content: stolen.content, tags: stolen.tags }, otherSk)]);
    sock.deliver(['EVENT', sock.subId, hostReply(req, { v: 1, id: 'someone-else', ok: true, result: { decoy: 'wrong id' } })]);
    const forged = hostReply(req, { v: 1, id: body.id, ok: true, result: { decoy: 'bad sig' } });
    forged.sig = '0'.repeat(128);
    sock.deliver(['EVENT', sock.subId, forged]);
    sock.deliver(['EVENT', sock.subId, hostReply(req, { v: 1, id: body.id, progress: { phase: 'list', pct: 50 } }, 7310)]);
    setTimeout(() => sock.deliver(['EVENT', sock.subId, hostReply(req, { v: 1, id: body.id, ok: true, result: { vms: [{ name: 'real' }] } })]), 20);
  });
  const rpc = R.createVmRpc(deps(WS));
  const out = await rpc.call(HOSTOBJ, 'vm.list', {}, { onProgress: p => progress.push(p) });
  assert.equal(out.ok, true, JSON.stringify(out));
  assert.deepEqual(out.result, { vms: [{ name: 'real' }] });
  assert.ok(order.indexOf('REQ') >= 0 && order.indexOf('REQ') < order.indexOf('EVENT'), 'subscribe BEFORE publish: ' + order);
  const req = WS.last.sent.find(m => m[0] === 'REQ');
  const ev = WS.last.sent.find(m => m[0] === 'EVENT')[1];
  assert.deepEqual(req[2]['#e'], [ev.id]);
  assert.deepEqual(req[2].authors, [HOST]);
  assert.deepEqual(progress, [{ phase: 'list', pct: 50 }]);
  rpc.closeAll();
  console.log('ok correlation');
}

// ---- silence is noAnswer, and a retry keeps the id --------------------------------------------
{
  const events = [];
  const WS = relayClass((m, sock) => { if (m[0] === 'EVENT'){ events.push(m[1]); sock.deliver(['OK', m[1].id, true, '']); } });
  const rpc = R.createVmRpc(deps(WS));
  const out = await rpc.call(HOSTOBJ, 'vm.list', {}, { retries: 1 });
  assert.equal(out.ok, false);
  assert.equal(out.noAnswer, true, 'silence must be reported as NO ANSWER');
  assert.ok(!('result' in out), 'no answer must not look like an empty result');
  assert.equal(events.length, 2, 'one retry');
  const ids = events.map(e => JSON.parse(dec(hostSk, USER, e.content)).id);
  assert.equal(ids[0], ids[1], 'the retry must carry the SAME idempotency id');
  assert.notEqual(events[0].id, events[1].id, 'the retry must be a NEW event');
  assert.equal(out.id, ids[0]);
  rpc.closeAll();
  console.log('ok noAnswer + retry id');
}

// ---- a retry that IS answered ------------------------------------------------------------------
{
  let n = 0;
  const WS = relayClass((m, sock) => {
    if (m[0] === 'REQ') sock.subId = m[1];
    if (m[0] !== 'EVENT') return;
    sock.deliver(['OK', m[1].id, true, '']);
    if (++n === 2){ const body = JSON.parse(dec(hostSk, USER, m[1].content));
      sock.deliver(['EVENT', sock.subId, hostReply(m[1], { v: 1, id: body.id, ok: true, result: { second: true } })]); }
  });
  const rpc = R.createVmRpc(deps(WS));
  const out = await rpc.call(HOSTOBJ, 'vm.power', { vm: 'x', action: 'start' }, { id: 'fixed-id', retries: 2 });
  assert.equal(out.ok, true); assert.deepEqual(out.result, { second: true }); assert.equal(n, 2);
  rpc.closeAll();
  console.log('ok retry answered');
}

// ---- relay refusal and host error ---------------------------------------------------------------
{
  let events = 0;
  const WS = relayClass((m, sock) => { if (m[0] === 'EVENT'){ events++; sock.deliver(['OK', m[1].id, false, 'blocked: vm request not addressed to this host']); } });
  const rpc = R.createVmRpc(deps(WS));
  const out = await rpc.call(HOSTOBJ, 'host.whoami', {}, { retries: 3 });
  assert.equal(out.ok, false); assert.ok(!out.noAnswer);
  assert.equal(out.error.code, 'relay_refused'); assert.match(out.error.message, /not addressed/);
  assert.equal(events, 1, 'a refusal is not retried');
  rpc.closeAll();

  const WS2 = relayClass((m, sock) => {
    if (m[0] === 'REQ') sock.subId = m[1];
    if (m[0] !== 'EVENT') return;
    const body = JSON.parse(dec(hostSk, USER, m[1].content));
    sock.deliver(['EVENT', sock.subId, hostReply(m[1], { v: 1, id: body.id, ok: false, error: { code: 'forbidden', message: 'only a host admin can do that' } })]);
  });
  const rpc2 = R.createVmRpc(deps(WS2));
  const out2 = await rpc2.call(HOSTOBJ, 'vm.create', { name: 'x' });
  assert.deepEqual(out2.error, { code: 'forbidden', message: 'only a host admin can do that' });
  rpc2.closeAll();

  const unreachable = class { constructor(){ throw new Error('nope'); } };
  const rpc3 = R.createVmRpc(deps(unreachable));
  const out3 = await rpc3.call(HOSTOBJ, 'host.whoami');
  assert.equal(out3.noAnswer, true);
  const bad = await rpc3.call({ pubkey: 'nope', relay: 'wss://x' }, 'host.whoami');
  assert.equal(bad.error.code, 'bad_host');
  console.log('ok refused + host error + unreachable');
}

// ---- a session signer signs the request AND reads the reply (phase 2) --------------------------
{
  const sessSk = new Uint8Array(32).fill(13), SESS = NT.getPublicKey(sessSk);
  let seen = null;
  const WS = relayClass((m, sock) => {
    if (m[0] === 'REQ') sock.subId = m[1];
    if (m[0] !== 'EVENT') return;
    seen = m[1];
    const body = JSON.parse(dec(hostSk, SESS, m[1].content));      // encrypted to the host FROM the session key
    const reply = NT.finalizeEvent({ kind: 6310, created_at: Math.floor(Date.now() / 1000),
      content: enc(hostSk, SESS, JSON.stringify({ v: 1, id: body.id, ok: true, result: { via: 'session' } })),
      tags: [['e', m[1].id], ['p', SESS]] }, hostSk);
    sock.deliver(['EVENT', sock.subId, reply]);
  });
  const rpc = R.createVmRpc(deps(WS));
  const signer = { pubkey: SESS, sign: async t => NT.finalizeEvent(t, sessSk),
                   enc: async (pk, t) => enc(sessSk, pk, t), dec: async (pk, c) => dec(sessSk, pk, c) };
  const out = await rpc.call(HOSTOBJ, 'vm.list', {}, { signer });
  assert.equal(out.ok, true, JSON.stringify(out));
  assert.equal(seen.pubkey, SESS, 'the request must be signed by the SESSION key, not the real one');
  assert.deepEqual(out.result, { via: 'session' });
  const plain = await rpc.call(HOSTOBJ, 'vm.list', {}, { retries: 0, timeout: 60 });
  assert.equal(seen.pubkey, USER, 'without a signer the real key signs');
  assert.equal(plain.noAnswer, true);
  rpc.closeAll();
  console.log('ok session signer');
}
console.log('ALL OK');
