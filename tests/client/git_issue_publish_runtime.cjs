/* A git issue that a relay STORED must never be reported as a failure, and a retry must never file
 * a second copy.
 *
 * Reported (git issues on the PosterChan repo): "At publish some issue on git I get the message
 * 'timeout' but the issue was published already" -- the reporter tapped again and FOUR identical
 * issues landed seconds apart; and Close/Resolve showed "timeout" + "relay: timeout" while the
 * issue did close. Measured on the relay: it answers OK in 1-4 ms; each event was stored, and the
 * phone simply did not have the OK within Relay.publish's eight seconds (a slow link, an OK queued
 * behind the socket's subscription backlog). Nothing else a repo is read from was even tried: the
 * issue never went to the relays the repo's own announcement names.
 *
 * Runs the SHIPPED relay.js and git.js under node. The fixtures are the sockets (per-relay
 * behaviour: answer, stay silent, answer late, refuse) and the DOM of the one form; `publish` is the
 * app.js contract (sign -> onSigned -> Relay.publish -> {ev, ...r}) reduced to what git.js uses.
 *
 * Usage: node git_issue_publish_runtime.cjs <relay.js> <git.js>   -> one JSON line of findings.
 */
'use strict';
const relayPath = process.argv[2], gitPath = process.argv[3];

// ---------- sockets ----------
const MODE = {};            // url -> { kind: 'ok'|'silent'|'lag'|'refuse', lag, why }
const FRAMES = [];          // every ['EVENT', ev] any socket was sent: [url, id]
class FakeWS {
  constructor(url){ this.url = url; this.readyState = 0; this.stored = new Map(); FakeWS.all.push(this);
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 0); }
  mode(){ return MODE[this.url.replace(/\/$/, '')] || { kind: 'ok' }; }
  reply(arr, lag){ setTimeout(() => { if(this.readyState === 1 && this.onmessage) this.onmessage({ data: JSON.stringify(arr) }); }, lag); }
  send(s){
    const m = JSON.parse(s), md = this.mode();
    if(m[0] === 'EVENT'){
      FRAMES.push([this.url, m[1].id]);
      if(md.kind === 'silent') return;
      if(md.kind === 'refuse'){ this.reply(['OK', m[1].id, false, md.why || 'blocked: no'], 5); return; }
      this.stored.set(m[1].id, m[1]);
      this.reply(['OK', m[1].id, true, ''], md.kind === 'lag' ? md.lag : 5);
    } else if(m[0] === 'REQ'){
      if(md.kind === 'silent') return;
      const lag = md.kind === 'lag' ? md.lag : 5;
      for(const f of m.slice(2)) for(const id of (f.ids || [])) if(this.stored.has(id)) this.reply(['EVENT', m[1], this.stored.get(id)], lag);
      this.reply(['EOSE', m[1]], lag);
    }
  }
  close(){ this.readyState = 3; }
}
FakeWS.all = [];
global.WebSocket = FakeWS; global.window = global; global.self = global;
global.Worker = class { constructor(){} postMessage(){} addEventListener(){} terminate(){} };
global.document = { addEventListener(){}, hidden: false, visibilityState: 'visible' };
global.location = { origin: 'https://x.test', protocol: 'https:', href: 'https://x.test/' };
try{ Object.defineProperty(global, 'navigator', { value: { onLine: true }, configurable: true }); }catch(_){}
global.indexedDB = { open(){ const r = {}; setTimeout(() => { r.onerror && r.onerror(); }, 0); return r; } };
const SAVED = [];
global.Store = { saveEvent(e){ SAVED.push(e.id); }, removeEvent(){}, get(){ return null; }, query(){ return []; } };
require(relayPath);
require(gitPath);
const Relay = global.Relay, sleep = ms => new Promise(r => setTimeout(r, ms));

// The shipped timings, shortened. Relay.publish keeps its own logic; only the budget is smaller.
const TIMEOUT = 1200;
const origPublish = Relay.publish.bind(Relay);
Relay.publish = (ev) => origPublish(ev, TIMEOUT);
Relay.LATE_MS = 8000;

// ---------- the one form's DOM ----------
let ROOT = null, CLOSED = 0;
const TOASTS = [];
function el(id){ return { id, value: '', textContent: '', disabled: false, onclick: null, onchange: null,
  focus(){}, addEventListener(){}, classList: { add(){}, remove(){} },
  click(){ if(!this.disabled && this.onclick) return this.onclick({ stopPropagation(){} }); } }; }
function modal(html, cb){
  const els = {};
  ROOT = { isConnected: true, els, addEventListener(){}, classList: { add(){}, remove(){} },
           q(sel){ return els[sel] || (els[sel] = el(sel)); } };
  cb(ROOT);
}
const $ = (sel, root) => root ? root.q(sel) : null;
const ME = 'b'.repeat(64);
let seq = 0;
async function publish(kind, content, tags, opts = {}){
  seq++;
  const ev = { id: (seq.toString(16) + 'e').padStart(64, 'a'), kind, content, tags, pubkey: ME,
               created_at: 1000 + seq, sig: (seq.toString(16)).padStart(128, 'f') };
  if(opts.onSigned) opts.onSigned(ev);
  const r = await Relay.publish(ev);
  if(!r.ok && !opts.quiet) TOASTS.push(r.msg);
  return { ev, ...r };
}
const git = window.PCGitFactory({
  state: { get ME(){ return { pubkey: ME }; }, get VIEW(){ return 'discover'; }, get GUEST(){ return false; },
           get CFG(){ return { relay_url: 'wss://pool.test' }; }, get LOGO(){ return ''; } },
  $, $$: () => [], modal, closeModal: () => { CLOSED++; if(ROOT) ROOT.isConnected = false; },
  enc: s => String(s), toast: m => TOASTS.push(String(m)), publish,
  attachMentionAutocomplete(){}, mentionTags: () => [], imetaTagsFor: () => [],
  _guestPrompt(){}, needProfile(){}, profOf: () => ({}),
});
const REPO = { id: 'c'.repeat(64), kind: 30617, pubkey: 'd'.repeat(64), created_at: 1, sig: 'e'.repeat(128), content: '',
  tags: [['d', 'demo'], ['name', 'demo'], ['relays', 'wss://pool.test', 'wss://repo.test']] };

function file(subject){
  git.newRepoIssue(REPO);
  const root = ROOT;
  root.q('#ri-subj').value = subject;
  root.q('#ri-body').value = 'The cover picker says my connection is down.';
  return root;
}
const press = root => root.q('#ri-pub').click();
const idsSent = () => [...new Set(FRAMES.map(f => f[1]))];
const reset = () => { FRAMES.length = 0; TOASTS.length = 0; SAVED.length = 0; CLOSED = 0; };
const said = root => [root.q('#ri-status').textContent, ...TOASTS].join(' | ');

(async () => {
  const out = {};
  Relay.configure({ urls: ['wss://pool.test'], verify: true });
  await sleep(30);

  // A. The pool never answers; the repo's own relay stores it. That is a published issue.
  { reset(); MODE['wss://pool.test'] = { kind: 'silent' }; MODE['wss://repo.test'] = { kind: 'ok' };
    const root = file('A: stored on the repo relay'); await press(root);
    out.repoStores = { closed: CLOSED, published: TOASTS.includes('issue published'), said: said(root),
                       reachedRepo: FRAMES.some(f => f[0].includes('repo.test')) }; }

  // B. The pool stores it but its OK is LATER than the timeout (the measured shape). The form says
  //    "not confirmed yet" -- never "timeout" -- and closes itself when the OK does arrive.
  { reset(); MODE['wss://pool.test'] = { kind: 'lag', lag: 3500 }; MODE['wss://repo.test'] = { kind: 'silent' };
    const root = file('B: late OK'); await press(root);
    out.late = { first: said(root), closedAtFirst: CLOSED };
    await sleep(2000);
    out.late.closedLater = CLOSED; out.late.published = TOASTS.includes('issue published');
    out.late.savedLocally = SAVED.length > 0; }

  // C. Nobody answers at all. A clear "not confirmed", the button comes back, and pressing again
  //    re-sends the SAME signed event -- to the pool and the repo relay alike.
  { reset(); MODE['wss://pool.test'] = { kind: 'silent' }; MODE['wss://repo.test'] = { kind: 'silent' };
    const root = file('C: silence');
    await press(root);
    out.silent = { first: said(root), enabled: !root.q('#ri-pub').disabled, closed: CLOSED };
    await press(root);
    out.silent.ids = idsSent().length; out.silent.frames = FRAMES.length;
    out.silent.second = said(root); }

  // D. Double tap: two presses before the first returns are ONE event.
  { reset(); MODE['wss://pool.test'] = { kind: 'ok' }; MODE['wss://repo.test'] = { kind: 'ok' };
    const root = file('D: double tap');
    const a = press(root), b = press(root); await Promise.all([a, b]);
    out.doubleTap = { ids: idsSent().length, poolFrames: FRAMES.filter(f => f[0].includes('pool.test')).length,
                      closed: CLOSED }; }

  // E. A real refusal from everyone is still reported as one, with the relay's own reason.
  { reset(); MODE['wss://pool.test'] = { kind: 'refuse', why: 'blocked: not in web of trust' };
    MODE['wss://repo.test'] = { kind: 'refuse', why: 'blocked: not in web of trust' };
    const root = file('E: refused'); await press(root);
    out.refused = { said: said(root), closed: CLOSED }; }

  // F. relay.js itself: silence resolves UNCONFIRMED with a `late` that a late OK settles true; a
  //    refusal is an answer with no `late`.
  { MODE['wss://pool.test'] = { kind: 'lag', lag: 1800 };
    const e = { id: 'f'.repeat(63) + '1', kind: 1, content: '', tags: [], pubkey: ME, created_at: 5, sig: '9'.repeat(128) };
    const r = await Relay.publish(e);
    out.relay = { ok: r.ok, msg: r.msg, unconfirmed: !!r.unconfirmed, hasLate: !!(r.late && r.late.then) };
    out.relay.late = r.late ? await r.late : null;
    MODE['wss://pool.test'] = { kind: 'refuse', why: 'blocked: nope' };
    const e2 = { ...e, id: 'f'.repeat(63) + '2' };
    const r2 = await Relay.publish(e2);
    out.relay.refused = [r2.ok, r2.msg, !!r2.unconfirmed]; }

  console.log(JSON.stringify(out)); process.exit(0);
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
