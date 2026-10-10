'use strict';
/* PRIVATE MUTES (NIP-51 encrypted content) — runs the SHIPPED publish(), fetchMutes(), _editPList(),
 * _readPrivateMutes(), _muteGuardOpts() and _persistMutes() from app.js under node.
 *
 * Reported (cryptowolf, muting a fediverse user): "safety: refused to erase your mute list" /
 * "replaceable-list shrink guard: 7<21". His newest kind-10000 carries 6 PUBLIC p-tags and a 19 KB
 * ENCRYPTED private section; the guard counted public tags only against an older public list of 21. */
const fs = require('fs'), path = require('path');
const acorn = require('/opt/flood/node_modules/acorn');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'app.js'), 'utf8');
const ast = acorn.parse(src, { ecmaVersion: 'latest', sourceType: 'script', allowReturnOutsideFunction: true });
const found = {};
(function walk(n){
  if(!n || typeof n.type !== 'string') return;
  if(n.type === 'FunctionDeclaration' && n.id && !found[n.id.name]) found[n.id.name] = src.slice(n.start, n.end);
  for(const k in n){ const v = n[k]; if(Array.isArray(v)) v.forEach(walk); else if(v && typeof v.type === 'string') walk(v); }
})(ast);
const need = ['_publishNow', 'fetchMutes', '_editPList', '_readPrivateMutes', '_muteGuardOpts', '_persistMutes'];
for(const n of need) if(!found[n]) throw new Error('app.js no longer has ' + n);

const pk = n => String(n).padStart(64, '0');
function world(o){
  const g = {};
  g.settings = Object.assign({}, o.settings || {});
  g.ClientSettings = { get: (k, d) => k in g.settings ? g.settings[k] : d, set: (k, v) => { g.settings[k] = v; } };
  g.ME = { pubkey: 'f'.repeat(64) }; g.GUEST = false;
  g.MUTED = new Set(); g.MUTED_WORDS = new Set(); g.MUTED_THREADS = new Set();
  g.MUTED_PRIVATE = new Set(); g._mutePrivateCount = null; g._mutePrivateOf = '';
  g.FOLLOWS = new Set(); g.VIEW = 'home';
  g.published = []; g.toasts = [];
  g.signer = o.signer;
  g.Relay = { query: async () => o.relay ? [o.relay] : [], publish: async () => ({ ok: true }) };
  g.Store = { query: () => o.store ? [o.store] : [], saveEvent(){}, removeEvent(){} };
  g.sign = async (kind, content, tags) => { const ev = { id: 'new', kind, content, tags, pubkey: g.ME.pubkey, created_at: 2000 }; g.published.push(ev); return ev; };
  g.toast = m => g.toasts.push(m); g.uiConfirm = async () => false;
  g._guestPrompt = () => {}; g._followSafetyMembers = () => [];
  g.InstEmoji = { loaded: true, SC_RE: /$a/ }; g._enrichTags = (k, t) => t;
  g.invalidateCounts = () => {}; g.applySobLive = () => {}; g._fediOnly = () => false;
  g._SOCIAL_KINDS = new Set([0, 1, 3, 6, 7]); g.window = {};
  g._syncAutoMutes = async () => {}; g._sameMembers = (a, b) => a.size === b.size && [...a].every(x => b.has(x));
  g._refreshTimelineMembership = () => {}; g.renderView = () => {};
  const names = Object.keys(g);
  const body = need.map(n => found[n]).join('\n') + '\nconst publish=_publishNow;\nreturn {' + need.join(',') + ', get:()=>({' +
    ['MUTED','MUTED_WORDS','MUTED_PRIVATE','_mutePrivateCount','settings','published','toasts'].map(x => x + ':' + (x === 'settings' || x === 'published' || x === 'toasts' ? 'G.' + x : x)).join(',') + '})};';
  const fnArgs = names.filter(n => !['settings','published','toasts'].includes(n));
  const make = new Function('G', ...fnArgs, body);
  return make(g, ...fnArgs.map(n => g[n]));
}
function ok(name, v){ if(!v) throw new Error(name); console.log('  ok   ' + name); }

(async () => {
  const pub6 = Array.from({ length: 6 }, (_, i) => ['p', pk(i + 1)]);
  const priv20 = Array.from({ length: 20 }, (_, i) => ['p', pk(100 + i)]).concat([['word', 'spoiler']]);
  const ditto = { id: 'd1', kind: 10000, pubkey: 'f'.repeat(64), created_at: 1000, content: 'CIPHER',
                  tags: pub6.concat([['client', 'Ditto']]) };
  const goodSigner = { nip44dec: async (p, c) => { if(c !== 'CIPHER') throw new Error('bad'); return JSON.stringify(priv20); },
                       nip44enc: async (p, t) => 'SEALED:' + t };
  const target = pk(999);

  // 1. The report: his mute now lands, the private section rides along untouched, nothing private leaks.
  let w = world({ settings: { mutedUsersCount: 21 }, relay: ditto, store: ditto, signer: goodSigner });
  await w.fetchMutes({ repaint: false });
  let s = w.get();
  ok('private mutes are applied (6 public + 20 private)', s.MUTED.size === 26 && s.MUTED_PRIVATE.size === 20);
  ok('a privately muted word is applied too', s.MUTED_WORDS.has('spoiler'));
  let r = await w._editPList(10000, target, true);
  s = w.get();
  const out = s.published.at(-1);
  ok('muting from PosterChan is no longer refused as "7<21"', r === true && !!out);
  const outP = out.tags.filter(t => t[0] === 'p').map(t => t[1]);
  ok('the new mute is published', outP.includes(target) && outP.length === 7);
  ok('no private mute is ever published as a public tag', !outP.some(p => /^0{61}1[01]\d$/.test(p) || Number.parseInt(p, 16) >= 100 && Number.parseInt(p, 16) < 120));
  ok('the encrypted private section is carried over unchanged', out.content === 'CIPHER');

  // 2. A signer that cannot decrypt: still allowed, because nothing in the newest list is lost.
  w = world({ settings: { mutedUsersCount: 21 }, relay: ditto, store: ditto,
              signer: { nip44dec: async () => { throw new Error('refused'); }, nip04dec: async () => { throw new Error('refused'); } } });
  await w.fetchMutes({ repaint: false });
  r = await w._editPList(10000, target, true);
  s = w.get();
  ok('without decryption a mute built on the newest list still goes out', r === true && s.published.at(-1).content === 'CIPHER');

  // 3. The guard still guards: an OLD short list from a lagging relay, while a newer long one is known.
  const long = { id: 'L', kind: 10000, pubkey: 'f'.repeat(64), created_at: 1500, content: '',
                 tags: Array.from({ length: 21 }, (_, i) => ['p', pk(i + 1)]) };
  const staleShort = { id: 'S', kind: 10000, pubkey: 'f'.repeat(64), created_at: 500, content: '', tags: pub6 };
  w = world({ settings: { mutedUsersCount: 21 }, relay: staleShort, store: long, signer: goodSigner });
  let threw = false;
  try{ await w._editPList(10000, target, true); }catch(e){ threw = /shrink guard/.test(String(e)); }
  ok('a publish from a stale short list is still refused', threw && w.get().published.length === 0);

  // 4. Unmuting somebody who was muted privately re-seals the private list without them.
  w = world({ settings: { mutedUsersCount: 21 }, relay: ditto, store: ditto, signer: goodSigner });
  await w.fetchMutes({ repaint: false });
  r = await w._editPList(10000, pk(100), false);
  s = w.get();
  const sealed = s.published.at(-1);
  const kept = JSON.parse(String(sealed.content).replace(/^SEALED:/, ''));
  ok('a private unmute goes out', r === true && /^SEALED:/.test(sealed.content));
  ok('the private list lost exactly that person', kept.length === 20 && !kept.some(t => t[1] === pk(100)));
  ok('and they were not added to the public tags', !sealed.tags.some(t => t[0] === 'p' && t[1] === pk(100)));
  ok('the local private set forgets them', !s.MUTED_PRIVATE.has(pk(100)));
  // 5. 2026-10-10: a NIP-46/55 signer showed "nip04_decrypt_failed: invalid base64". A NIP-44 private list whose
  //    NIP-44 decrypt failed was handed to nip04_decrypt anyway -- which can only fail, LOUDLY, in the signer.
  //    NIP-04 is only ever asked for content that IS NIP-04 (`?iv=`).
  {
    const asked = [];
    const sg = { nip44dec: async () => { asked.push('44'); throw new Error('refused'); },
                 nip04dec: async () => { asked.push('04'); throw new Error('nip04_decrypt_failed: invalid base64'); } };
    const ww = world({ settings: {}, signer: sg });
    const res = await ww._readPrivateMutes({ kind: 10000, content: 'AsomeNip44PayloadWithoutAnIv==', tags: [] });
    ok('NIP-44 content is never sent to the NIP-04 decryptor', res === null && asked.join(',') === '44');
    asked.length = 0;
    await ww._readPrivateMutes({ kind: 10000, content: 'abc?iv=def', tags: [] });
    ok('NIP-04 content still goes to the NIP-04 decryptor', asked.includes('04') && !asked.includes('44'));
  }
  console.log('OK private mutes');
})().catch(e => { console.error(e.stack || e); process.exitCode = 1; });
