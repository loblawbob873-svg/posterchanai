/* Run the SHIPPED static/js/client/sms.js under node against a stub phone and a stub relay.
 *
 * Why a simulator and not a grep: everything worth checking here is a RELATIONSHIP between two
 * calls, not a string.
 *
 *   * a delete is two deletes, and the ORDER decides whether a failure resurrects the message;
 *   * the high-water mark may only move past messages that actually landed, or a relay that stops
 *     taking writes silently skips the rest of somebody's history;
 *   * a send another device asked for must be marked done even when it FAILED, because the
 *     alternative is a phone that re-sends it on every drain and there is no way to un-send a text.
 *
 * There is no DOM: `paint()` returns immediately unless the Texts view is on screen, and nothing
 * else here touches `document`, so the view is left as something else and every path under test is
 * the real one.
 *
 * Usage:  node sms_sim.js '<json options>'   → prints a JSON transcript on stdout.
 */
const path = require('path');
const { webcrypto } = require('crypto');
const ROOT = path.resolve(__dirname, '..', '..');

const opt = JSON.parse(process.argv[2] || '{}');

const calls = [];        // every plugin call, in order
const published = [];    // every event handed to publish(), in order
const notified = [];

/* ---- the stub phone -------------------------------------------------------------------------- */

// The system message store. `doc` is what SmsPlugin computes on a real handset (SmsKeys.docId); the
// sim carries it the same way, because the client never derives it and must not start.
let rows = (opt.rows || []).slice();
const isPhone = () => opt.isPhone !== false;

// READ_SMS is a RUNTIME grant and is not implied by the SMS role — two switches, and the app used
// to conflate them. `canRead` starts false unless the option says otherwise, which is the state
// every real phone is in the first time somebody opens Texts.
// Default: a handset that is the default SMS app has been granted it (that is the ordinary,
// working state every archive test below is about). `canRead:false` is the state a real phone is in
// the FIRST time somebody opens Texts, which is what the permission tests exercise.
let canRead = opt.canRead === undefined ? isPhone() : opt.canRead === true;

const PLUGIN = {
  async status(){
    calls.push(['status', isPhone(), canRead]);
    // AN OLDER APK HAS NO `canRead` AND NO `ensureRead`. Absent is "it never asked", and on those
    // builds reading was gated on the role — so the role is the honest answer to give, and the
    // client must not read `undefined` as "not allowed" and hide a working screen behind a button
    // that cannot exist.
    if(opt.oldApk) return { isDefault: isPhone(), unread: 0, mms: false };
    return { isDefault: isPhone(), canRead, unread: 0, mms: false };
  },
  async list(a){
    calls.push(['list', a && a.since || 0]);
    // THE PROVIDER REFUSES WITHOUT READ_SMS, and `SmsStore.query` turns that refusal into an empty
    // list — which is exactly what a phone with no texts returns. That indistinguishability IS the
    // bug, so the stub reproduces it rather than handing the client rows it could never have had.
    // An older APK had no permission concept and gated reading on the role; it behaves that way.
    const allowed = opt.oldApk ? isPhone() : canRead;
    if(!allowed) return { messages: [] };
    const since = (a && a.since) || 0;
    return { messages: rows.filter(r => r.date > since).sort((x, y) => x.date - y.date) };
  },
  async send(a){
    calls.push(['send', a.to, a.body]);
    if(opt.sendFails) return { ok:false, error:'radio said no', parts:1, row:'' };
    const m = { id: 900 + rows.length, thread: 1, address: a.to, body: a.body,
                date: Date.now(), type: 2, incoming: false, read: true,
                doc: 'pcai:sms:sent' + rows.length };
    rows.push(m);
    return { ok:true, error:'', parts:1, row:'content://sms/' + m.id };
  },
  async delete(a){
    calls.push(['delete', (a.ids || []).slice()]);
    if(opt.deleteFails) return { deleted: 0 };
    const gone = new Set(a.ids || []);
    const before = rows.length;
    rows = rows.filter(r => !gone.has(r.id));
    return { deleted: before - rows.length };
  },
  async markRead(){ return { marked: 0 }; },
  async deleteThread(){ return { deleted: 0 }; },
  async nameFor(){ return { name: '' }; },
};

if(!opt.oldApk) PLUGIN.ensureRead = async function(){
  calls.push(['ensureRead']);
  if(opt.grantOnAsk) canRead = true;
  return { granted: canRead };
};

/* ---- the stub relay -------------------------------------------------------------------------- */

// Events the relay holds, keyed by `d`. An addressable event has exactly one newest version, which
// is the property the whole archive design leans on — so the store keeps one per address.
const relay = new Map();
if(opt.relay) for(const ev of opt.relay) relay.set(dOf(ev), ev);

function dOf(ev){ return ((ev.tags || []).find(t => t[0] === 'd') || [])[1] || ''; }

let refusals = Number(opt.refuseAfter != null ? opt.refuseAfter : -1);

global.window = global;
global.crypto = webcrypto;
global.document = { addEventListener(){}, querySelector(){ return null; } };
global.localStorage = (() => {
  const m = Object.assign({}, opt.storage || {});
  return { getItem: k => (k in m ? String(m[k]) : null),
           setItem: (k, v) => { m[k] = String(v); },
           _all: m };
})();

/* OBJECTS, not functions. The client says `const Store = () => window.Store` and then
 * `Store().query(...)` — so `window.Store` is the object itself. A stub that was a factory made
 * `.query` undefined, every read threw into its own catch, and every test failed as though the
 * device simply had no messages. */
global.Store = { query: () => (opt.cached || []).slice() };
global.Relay = {
  async query(){
    if(opt.relayDown) throw new Error('no relay');
    // An UNREACHABLE relay and an EMPTY one are different answers, and the archive must treat them
    // differently — this is the switch that lets a test show it.
    return opt.relayEmpty ? [] : Array.from(relay.values());
  },
  subscribe(){ return { id: 'sub' }; },
  close(){},
};

global.__PC = {
  VIEW: 'timeline',                 // NOT texts, so paint() is a no-op and needs no DOM
  ME: { pubkey: 'me' },
  $: () => null,
  enc: s => String(s == null ? '' : s),
  toast(){},
  uiConfirm: async () => true,
  uiPrompt: async () => '',
  switchView(){},
  capPlugin: (name, method) => (name === 'Sms' && PLUGIN[method || 'status']) ? PLUGIN : null,
  osNotify: (title, body) => { notified.push([String(title), String(body)]); },
  // A transparent "encryption": the sim is about the protocol, not the cipher, and a readable
  // transcript is what makes a wrong body visible in a failure message.
  nip44enc: async (pk, s) => 'enc:' + s,
  nip44dec: async (pk, ct) => {
    if(String(ct).slice(0, 4) !== 'enc:') throw new Error('not ours');
    return String(ct).slice(4);
  },
  async publish(kind, content, tags, o){
    const d = ((tags || []).find(t => t[0] === 'd') || [])[1] || '';
    published.push({ kind, d, content });
    if(kind === 5) return { ok:true, ev:{ kind, tags } };
    if(refusals === 0){ return { ok:false, ev:null }; }
    if(refusals > 0) refusals--;
    const ev = { kind, content, tags, created_at: Math.floor(Date.now() / 1000) + published.length,
                 pubkey:'me', id:'e' + published.length };
    if(content) relay.set(d, ev); else relay.delete(d);
    return { ok:true, ev };
  },
};

require(path.join(ROOT, 'static', 'js', 'client', 'sms.js'));

(async () => {
  const S = global.PCSms;
  for(const step of (opt.steps || ['load'])){
    if(step === 'load'){ await S.load(true); }
    else if(step === 'mirror'){ await S.mirror(); }
    else if(step === 'drain'){ await S.drainOutbox(); }
    else if(step === 'allow'){ refusals = -1; }
    else if(step.slice(0, 5) === 'send:'){
      const [to, ...rest] = step.slice(5).split(':');
      const r = await S.send(to, rest.join(':'));
      calls.push(['sendResult', r.ok, r.where || r.error]);
    }
    else if(step.slice(0, 7) === 'remove:'){
      const r = await S.remove(step.slice(7).split(','));
      calls.push(['removeResult', r.archive, r.phone]);
    }
    else if(step === 'render'){ await S.render(); }
    else if(step === 'why'){
      const w = await S.emptyWhy();
      calls.push(['why', w.fix || '', w.why]);
    }
    else if(step === 'settle'){ await new Promise(r => setTimeout(r, 20)); }
  }
  await new Promise(r => setTimeout(r, 20));
  const st = S._state();
  console.log(JSON.stringify({
    calls, published, notified,
    rows: rows.map(r => r.doc),
    relay: Array.from(relay.keys()).sort(),
    // LIVE documents only. A tombstone is kept in the map as a marker (see sms.js absorb) so an
    // older cached copy cannot walk over the hole and restore the message; what the person sees is
    // the set below.
    docs: Array.from(st.msgs.values()).filter(m => !m.gone).map(m => m.doc).sort(),
    threads: st.threads.map(t => ({ key: t.key, n: t.msgs.length })),
    hwm: Object.keys(global.localStorage._all)
              .filter(k => k.indexOf('pc_sms_hwm') === 0)
              .map(k => Number(global.localStorage._all[k]))[0] || 0,
  }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
