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
const uploads = Object.create(null);
for(const [sha, item] of Object.entries(opt.uploads || {})) uploads[sha] = Object.assign({}, item);
const driveFolders = new Set();
const driveFiles = [];
let driveBatch = 0;

/* ---- the stub phone -------------------------------------------------------------------------- */

// The system message store. `doc` is what SmsPlugin computes on a real handset (SmsKeys.docId); the
// sim carries it the same way, because the client never derives it and must not start.
let rows = (opt.rows || []).slice();
// Large-provider regression inputs stay generated inside the simulator. Passing two thousand full
// MMS rows through argv exceeds Linux's per-argument bound before Node gets a chance to test them.
if(opt.generatedPictures){
  const base = Number(opt.now) || Date.now();
  for(let i=1;i<=Number(opt.generatedPictures);i++) rows.push({
    id:i,thread:1,address:'+15550100',body:'',date:base-i*60000,type:1,incoming:true,
    read:true,mms:true,parts:[{id:900+i,ct:'image/jpeg',name:'p'+i+'.jpg',bytes:2048}],
    doc:'pcai:sms:generated'+String(i).padStart(12,'0')
  });
}
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
    return { isDefault: isPhone(), canRead, unread: 0, mms: false,
             // What Android NAMED, which is what the screen quotes back. A bare boolean left the
             // person arguing with a sentence: "android keeps saying posterchan is not the phones
             // messaging app but I see all my texts".
             defaultPackage: opt.defaultPkg === undefined
                 ? (isPhone() ? 'place.poster.app' : '') : opt.defaultPkg,
             package: 'place.poster.app',
             roleHeld: opt.roleHeld === true,
             telephony: opt.telephony !== false };
  },
  async list(a){
    calls.push(['list', a && a.since || 0, a && a.before || 0]);
    // THE PROVIDER REFUSES WITHOUT READ_SMS, and `SmsStore.query` turns that refusal into an empty
    // list — which is exactly what a phone with no texts returns. That indistinguishability IS the
    // bug, so the stub reproduces it rather than handing the client rows it could never have had.
    // An older APK had no permission concept and gated reading on the role; it behaves that way.
    const allowed = opt.oldApk ? isPhone() : canRead;
    if(!allowed) return { messages: [] };
    const since = (a && a.since) || 0;
    const before = (a && a.before) || 0;
    // THE TWO PROVIDER TABLES REFUSE INDEPENDENTLY — several OEM builds guard `content://mms`
    // differently from `content://sms`. Folded into `refused` an MMS-only refusal reads as "you
    // have no messages", over a full inbox.
    let found = rows.filter(r => before ? r.date < before : r.date > since)
                    .sort((x, y) => before ? y.date - x.date : x.date - y.date);
    // Captured failure shape: the combined timeline still returns all SMS text rows, but its
    // interleaving/truncation never surfaces historical MMS rows. listMms below represents the
    // direct content://mms audit added for that already-deployed state.
    if(opt.combinedOmitsMms) found = found.filter(r => !r.mms);
    if(a && a.limit){
      let want = Math.max(1, Number(a.limit) || 1);
      // Android's MMS reader caps one provider query. Asking for a larger first page never exposes
      // the older media; only strict `before` pages cross the boundary.
      if(opt.providerPageCap) want = Math.min(want, Math.max(1, Number(opt.providerPageCap) || 1));
      found = found.slice(0, want);
    }
    const out = { messages: found };
    if(opt.mmsRefused) out.mmsRefused = true;
    // TRUNCATED, not exhausted — MmsStore.MAX_ROWS hands back the newest 2,000 and there is no way
    // to ask for the rest. The stub reports it the way the plugin does, on every reply.
    if(opt.mmsCapped || (opt.providerPageCap && a && Number(a.limit) > Number(opt.providerPageCap)
                         && found.length >= Number(opt.providerPageCap))) out.mmsCapped = true;
    return out;
  },
  async listMms(a){
    calls.push(['listMms', a && a.before || 0]);
    if(!canRead) return { messages: [], mmsRefused:true };
    const before = (a && a.before) || 0;
    let found = rows.filter(r => r.mms && (!before || r.date < before))
                    .sort((x, y) => y.date - x.date);
    if(a && a.limit) found = found.slice(0, Math.max(1, Number(a.limit) || 1));
    return { messages:found };
  },
  /* WHAT THIS CARRIER SAYS AN MMS MAY WEIGH. `opt.mmsLimit` sets it; `opt.noMmsLimit` models an
   * older APK with no such method, which must fall back rather than refuse to send. */
  async mmsLimit(){
    calls.push(['mmsLimit']);
    if(opt.noMmsLimit) throw new Error('no such method');
    return { bytes: Number(opt.mmsLimit) || 300 * 1024, measured: opt.mmsMeasured !== false };
  },
  async sendMms(a){
    calls.push(['sendMms', a.to, a.body, (a.data || '').length, a.mime, a.name]);
    if(opt.sendFails) return { ok:false, error:'radio said no' };
    return { ok:true, error:'', stored: opt.mmsStored !== false, row:'content://mms/1' };
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
    // BOTH LISTS, RECORDED SEPARATELY. A picture message lives at `content://mms/<id>` and a client
    // that sends every id down the SMS path deletes nothing and reports nothing — which the client
    // reads as a provider refusal, so the archive is left alone and the delete quietly did not
    // happen. The transcript has to be able to show which list each id went into.
    calls.push(['delete', (a.ids || []).slice(), (a.mmsIds || []).slice()]);
    if(opt.deleteFails) return { deleted: 0 };
    const sms = new Set(a.ids || []), mms = new Set(a.mmsIds || []);
    const before = rows.length;
    // The stub is the PROVIDER, so it enforces the provider's rule: an id offered down the wrong
    // path removes nothing at all.
    let left = opt.deleteLimit === undefined ? Infinity : Math.max(0, Number(opt.deleteLimit) || 0);
    rows = rows.filter(r => {
      const hit = r.mms ? mms.has(r.id) : sms.has(r.id);
      if(hit && left > 0){ left--; return false; }
      return true;
    });
    return { deleted: before - rows.length };
  },
  /* ONE ATTACHMENT'S BYTES. Three answers, not two: the data, "too large for the bridge" (a real
   * file somebody can still open in their gallery), and a refusal — rendered identically they are
   * all a broken image and only some of them are fixable. */
  async attachment(a){
    calls.push(['attachment', a && a.part]);
    const part = (opt.parts || {})[String(a && a.part)];
    const known = rows.some(r => (r.parts || []).some(p => Number(p.id) === Number(a && a.part)));
    if(opt.chunked && a && a.offset !== undefined && (part || known)){
      const all = Buffer.from((part && part.data) || 'eA==', 'base64');
      const size = Math.max(1, Number(opt.chunkSize) || Number(a.max) || 1);
      const at = Math.max(0, Number(a.offset) || 0);
      const piece = all.subarray(at, at + size);
      return { offset:at, data:piece.toString('base64'), bytes:piece.length,
               total:all.length, done:at + piece.length >= all.length };
    }
    if(!part && known) return { data: 'eA==', bytes: 1 };
    if(!part) return { data: '', tooBig: false };
    if(part.tooBig) return { data: '', tooBig: true };
    return { data: part.data || '', bytes: (part.data || '').length };
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
if(opt.oldApk) delete PLUGIN.listMms;

/* ---- the stub relay -------------------------------------------------------------------------- */

// Events the relay holds, keyed by `d`. An addressable event has exactly one newest version, which
// is the property the whole archive design leans on — so the store keeps one per address.
const relay = new Map();
if(opt.relay) for(const ev of opt.relay) relay.set(dOf(ev), ev);

function dOf(ev){ return ((ev.tags || []).find(t => t[0] === 'd') || [])[1] || ''; }

let refusals = Number(opt.refuseAfter != null ? opt.refuseAfter : -1);

global.window = global;
global.crypto = webcrypto;
global.ICO = () => '';
const documentListeners = new Map();
global.document = {
  visibilityState: 'visible',
  addEventListener(name, fn){
    const a = documentListeners.get(name) || []; a.push(fn); documentListeners.set(name, a);
  },
  async _fire(name){
    for(const fn of (documentListeners.get(name) || []).slice()) await fn();
  },
  querySelector(){ return null; }
};
global.localStorage = (() => {
  const m = Object.assign({}, opt.storage || {});
  return { getItem: k => (k in m ? String(m[k]) : null),
           setItem: (k, v) => { m[k] = String(v); },
           // A REAL localStorage HAS THIS, and the archive's re-scan clears its latches with it.
           // Absent from the stub, `removeItem` threw into the client's own catch and the reset was
           // a silent no-op -- the sim agreeing with a bug rather than showing it.
           removeItem: k => { delete m[k]; },
           _all: m };
})();

/* OBJECTS, not functions. The client says `const Store = () => window.Store` and then
 * `Store().query(...)` — so `window.Store` is the object itself. A stub that was a factory made
 * `.query` undefined, every read threw into its own catch, and every test failed as though the
 * device simply had no messages. */
global.Store = { query: () => {
  calls.push(['storeQuery']);
  if(opt.coldLoadSignerFailure) return { sort(){
    throw new Error('invalid plaintext size: must be between 1 and 65535 bytes');
  } };
  return (opt.cached || []).slice();
} };
global.Relay = {
  async query(){
    if(opt.relayDown) throw new Error('no relay');
    // An UNREACHABLE relay and an EMPTY one are different answers, and the archive must treat them
    // differently — this is the switch that lets a test show it.
    return opt.relayEmpty ? [] : Array.from(relay.values());
  },
  subscribe(filters, handlers){
    global.__smsLiveEvent = handlers && handlers.onEvent;
    return { id: 'sub' };
  },
  close(){},
};

const filesIdx = {
  async pull(){ calls.push(['drivePull']); },
  folders(){ return Array.from(driveFolders); },
  addFolder(name, enc){ driveFolders.add(String(name)); calls.push(['folder', name, enc]); },
  beginBatch(){ driveBatch++; calls.push(['driveBegin', driveBatch]); },
  async endBatch(){ driveBatch--; calls.push(['driveEnd', driveBatch]); return true; },
};
global.window = global.window || {};
global.window.__PC_API_BASE__ = (opt.apiBase === undefined ? 'https://node.example' : opt.apiBase);
const visibleFeed = opt.desktopOwnershipRace ? {
  innerHTML:'<div class="spinner"></div>', className:'feed feed-texts',
  querySelectorAll(){ return []; }, querySelector(){ return null; }
} : null;
if(opt.desktopOwnershipRace) global.PCOS = {
  isOn: () => true,
  // Reproduce the feed-handoff turn: the route callback is active, but OS bookkeeping has not yet
  // made the conservative background-paint predicate true.
  ownsFeedView: () => false,
};
global.__PC = {
  VIEW: opt.desktopOwnershipRace ? 'texts' : 'timeline',
  ME: { pubkey: 'me' },
  $: sel => sel === '#feed' ? visibleFeed : null,
  enc: s => String(s == null ? '' : s),
  toast(){},
  uiConfirm: async () => true,
  uiPrompt: async () => '',
  switchView(){},
  capPlugin: (name, method) => (name === 'Sms' && PLUGIN[method || 'status']) ? PLUGIN : null,
  osNotify: (title, body) => { notified.push([String(title), String(body)]); },
  filesIdx: () => filesIdx,
  uploadEncFile: async (file, folder) => {
    calls.push(['uploadEncFile', file.name, folder]);
    const bytes = new Uint8Array(await file.arrayBuffer());
    const sha = Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex');
    uploads[sha] = { folder, name:file.name, type:file.type,
                     text:Buffer.from(bytes).toString('utf8') };
    driveFolders.add(String(folder));
    driveFiles.push({ sha, folder:String(folder), name:String(file.name) });
    return sha;
  },
  encFileUrl: async sha => { calls.push(['encFileUrl', sha]); return 'blob:' + sha; },
  /* Encrypt-and-upload for somebody who is NOT us. The real one (app.js) mints a random AES key,
     uploads the ciphertext and returns `<blobUrl>#pcenc1=<b64u(JSON{k,m,n})>`. The sim reproduces
     the SHAPE exactly -- the shape is what sms.js parses to build the recipient's link -- with a
     fixed key, because what is under test is the link, not the cipher. */
  uploadSharedEnc: async (file) => {
    calls.push(['uploadSharedEnc', file.name, file.size]);
    if(opt.uploadFails) throw new Error('blossom said no');
    const bytes = new Uint8Array(await file.arrayBuffer());
    const sha = Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex');
    const meta = Buffer.from(JSON.stringify({ k:'a'.repeat(43), m:file.type || '', n:file.name || '' }))
                   .toString('base64').replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
    const host = opt.blobHost || 'https://node.example';
    return host + '/blossom/' + sha + '.enc#pcenc1=' + meta;
  },
  // A transparent "encryption": the sim is about the protocol, not the cipher, and a readable
  // transcript is what makes a wrong body visible in a failure message.
  nip44enc: async (pk, s) => 'enc:' + s,
  nip44dec: async (pk, ct) => {
    if(String(ct).slice(0, 4) !== 'enc:') throw new Error('not ours');
    const plaintext = String(ct).slice(4);
    if((opt.rejectInvalidPlaintext === true) &&
       (Buffer.byteLength(plaintext, 'utf8') < 1 || Buffer.byteLength(plaintext, 'utf8') > 65535))
      throw new Error('invalid plaintext size: must be between 1 and 65535 bytes');
    for(const [needle, ms] of Object.entries(opt.decryptDelays || {}))
      if(String(ct).includes(needle)) await new Promise(r => setTimeout(r, Number(ms) || 0));
    return plaintext;
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
global.fetch = async url => {
  const sha = String(url || '').replace(/^blob:/, '');
  const item = uploads[sha];
  if(!item) return { ok:false, async json(){ throw new Error('missing encrypted blob ' + sha); } };
  /* encFileUrl returns a URL consumed as bytes by attachment rendering/retry, and as JSON by the
   * encrypted message envelope. A browser Response supports both. Giving the simulator only json()
   * made the shipped Web/OS attachment path look broken in tests even though the browser can open
   * it, and left the old-media URL itself completely unexercised. */
  return { ok:true,
           async json(){ return JSON.parse(item.text); },
           async blob(){ return new Blob([Buffer.from(item.text)],
                              {type:item.type || 'application/octet-stream'}); } };
};

require(path.join(ROOT, 'static', 'js', 'client', 'sms.js'));

(async () => {
  const S = global.PCSms;
  for(const step of (opt.steps || ['load'])){
    if(step === 'load'){ await S.load(true); }
    else if(step === 'phoneLoad'){ await S.loadFromPhone(); }
    else if(step === 'migrate'){ await S.mirror({ fullMigration:true, limit:Number(opt.migrationBatch)||60 }); }
    // The whole batched loop, the way render() drives it — the only way to see it converge (or not).
    else if(step === 'migrateAll'){ calls.push(['migrateAll', await S.migrateAll()]); }
    else if(step === 'importAll'){ calls.push(['importAll', await S.importAll()]); }
    else if(step === 'appendRows'){ rows.push(...(opt.appendRows || [])); }
    // The deliberate, person-pressed re-read: clears the archive's latches and walks the phone again.
    else if(step === 'rescan'){ calls.push(['rescan', await S.rescan()]); }
    else if(step === 'mirror'){ await S.mirror(); }
    else if(step === 'drain'){ await S.drainOutbox(); }
    else if(step === 'allow'){ refusals = -1; }
    // Resume the real foreground handler. This is deliberately not a direct migrateAll call: the
    // regression was that visibility only ran the recent timestamp sweep after an interrupted
    // historical migration, so the older tail could never be reached again.
    else if(step === 'foreground'){ await document._fire('visibilitychange'); }
    /* `sendfile:<to>:<bytes>` — a send with an attachment of a given size, which is the only input
       the oversized-link decision actually turns on. */
    else if(step.slice(0, 9) === 'sendfile:'){
      const [to, size, ...rest] = step.slice(9).split(':');
      const bytes = new Uint8Array(Number(size) || 0);
      const f = new File([bytes], 'photo.jpg', { type:'image/jpeg' });
      const r = await S.send(to, rest.join(':'), f);
      calls.push(['sendFileResult', r.ok, r.where || r.error || '', r.link || '']);
    }
    /* Extension-only video from a desktop/network picker. File.type is genuinely empty on some
       platforms, so this exercises production MIME inference and the remote-phone outbox. */
    else if(step.slice(0, 10) === 'sendvideo:'){
      const [to, size, ...rest] = step.slice(10).split(':');
      const bytes = new Uint8Array(Number(size) || 0);
      const f = new File([bytes], 'clip.mp4', { type:'' });
      const r = await S.send(to, rest.join(':'), f);
      calls.push(['sendVideoResult', r.ok, r.where || r.error || '']);
    }
    else if(step.slice(0, 5) === 'send:'){
      const [to, ...rest] = step.slice(5).split(':');
      const r = await S.send(to, rest.join(':'));
      calls.push(['sendResult', r.ok, r.where || r.error]);
    }
    else if(step.slice(0, 7) === 'remove:'){
      const r = await S.remove(step.slice(7).split(','));
      calls.push(['removeResult', r.archive, r.phone]);
    }
    else if(step === 'removePending'){
      const m = Array.from(S._state().msgs.values()).find(x => x && x.pending);
      const r = await S.remove(m ? [m.doc] : []);
      calls.push(['removePendingResult', r.archive || 0, r.phone || 0, r.cancelled || 0]);
    }
    else if(step === 'retryFailed'){
      const m = Array.from(S._state().msgs.values()).find(x => x && x.failed);
      const r = await S._retryFailed(m);
      calls.push(['retryFailedResult', !!(r && r.ok),
                  (r && (r.where || r.error)) || '', (r && r.warning) || '']);
    }
    else if(step === 'render'){ await S.render(); }
    /* A cold route and the desktop focus callback can arrive in the same turn. They must share the
       first cache transaction rather than racing two partial paints. */
    else if(step === 'concurrentRenderFocus'){
      await Promise.all([S.render(), document._fire('visibilitychange')]);
    }
    else if(step === 'why'){
      const w = await S.emptyWhy();
      calls.push(['why', w.fix || '', w.why]);
    }
    else if(step === 'settle'){ await new Promise(r => setTimeout(r, 20)); }
    else if(step === 'absorbRaw'){ await S._absorb(opt.rawEvents || []); }
    else if(step === 'liveEvent'){
      if(typeof global.__smsLiveEvent !== 'function') throw new Error('Texts live subscription missing');
      await global.__smsLiveEvent((opt.rawEvents || [])[0]);
    }
    else if(step === 'absorbConcurrent'){
      await Promise.all((opt.rawEvents || []).map(ev => S._absorb([ev])));
    }
  }
  await new Promise(r => setTimeout(r, 20));
  const st = S._state();
  let scrollProbe = null;
  if(opt.scrollProbe){
    const reading = {scrollTop:240, scrollHeight:1200, clientHeight:400};
    const savedReading = S._scrollState(reading);
    reading.scrollTop = 0; S._putScroll(reading, savedReading);
    const pinned = {scrollTop:795, scrollHeight:1200, clientHeight:400};
    const savedPinned = S._scrollState(pinned);
    pinned.scrollHeight = 1600; pinned.scrollTop = 0; S._putScroll(pinned, savedPinned);
    scrollProbe = {savedReading, restoredReading:reading.scrollTop,
                   savedPinned, restoredPinned:pinned.scrollTop};
  }
  let hydrationProbe = null;
  if(opt.hydrationProbe){
    const anchor={offsetTop:300,offsetHeight:80,isConnected:true};
    const list={scrollTop:240,scrollHeight:1200,clientHeight:400,
      querySelectorAll(){return [anchor];},contains(el){return el===anchor;}};
    const below=S._hydrationScrollState(list);
    list.scrollHeight=1400; S._restoreHydratedScroll(list,below);
    const belowTop=list.scrollTop;
    list.scrollTop=240;list.scrollHeight=1200;anchor.offsetTop=300;
    const above=S._hydrationScrollState(list);
    anchor.offsetTop=500;list.scrollHeight=1400;S._restoreHydratedScroll(list,above);
    hydrationProbe={belowTop,aboveTop:list.scrollTop};
  }
  console.log(JSON.stringify({
    calls, published, notified, uploads, scrollProbe, hydrationProbe,
    feedHtml: visibleFeed ? visibleFeed.innerHTML : '',
    drive: { folders:Array.from(driveFolders).sort(), files:driveFiles, batch:driveBatch },
    rows: rows.map(r => r.doc),
    relay: Array.from(relay.keys()).sort(),
    relayEvents: Array.from(relay.entries()).map(([d, e]) => ({d, content:e.content})),
    // LIVE documents only. A tombstone is kept in the map as a marker (see sms.js absorb) so an
    // older cached copy cannot walk over the hole and restore the message; what the person sees is
    // the set below.
    docs: Array.from(st.msgs.values()).filter(m => !m.gone).map(m => m.doc).sort(),
    threads: st.threads.map(t => ({ key: t.key, n: t.msgs.length,
                                   // The order a thread is READ in, so a merge that interleaves
                                   // wrongly is visible rather than merely counted.
                                   order: t.msgs.map(m => m.doc),
                                   bodies: t.msgs.map(m => m.body || ''),
                                   parts: t.msgs.map(m => (m.parts || []).length),
                                   partIds: t.msgs.map(m => (m.parts || []).map(p => Number(p.id)||0)),
                                   partShas: t.msgs.map(m => (m.parts || []).map(p => String(p.sha||''))),
                                   pending: t.msgs.map(m => !!m.pending),
                                   failed: t.msgs.map(m => !!m.failed) })),
    mmsRefused: !!st.mmsRefused,
    mmsCapped: !!st.mmsCapped,
    blossomDone: !!global.localStorage._all[Object.keys(global.localStorage._all)
                    .filter(k => /_blossom_v\d+$/.test(k))[0]],
    /* THE MARK ITSELF, not whichever `pc_sms_hwm*` key happens to come first. The marker keys are
     * siblings of it (`_blossom_v6`, `_blossom_rewound_v4`, `_oldest_first_v1`) and all hold "1",
     * so an unanchored filter reported a high-water mark of 1 -- a plausible-looking number that
     * makes any "did the mark move" assertion pass for the wrong reason. */
    hwm: Object.keys(global.localStorage._all)
              .filter(k => /^pc_sms_hwm_[^_]+$/.test(k))
              .map(k => Number(global.localStorage._all[k]))[0] || 0,
  }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
