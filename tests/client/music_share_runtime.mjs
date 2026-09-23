// Runs the SHIPPED static/js/client/musicshare.js for three people — A shares, B receives, C is a
// stranger — against one in-memory relay and one in-memory Blossom that keeps per-blob OWNERS the
// way the real one does (a DELETE drops one reference; the bytes go with the last). NIP-44 is the
// real nostr-tools implementation and every cipher is real WebCrypto, so "C cannot read it" and "B's
// added copy still plays" are measured, not assumed. The library record B ends up with is decrypted
// by app.js's OWN `_driveDecrypt`, lifted out of the shipped file — that is the function the player
// will call on it.
//
// Prints one JSON object: { scenario: {ok, detail} }.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { webcrypto } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { clientSource, clientSourceAt, installStateGlobals } from './client_source.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const SHARE_JS = fs.readFileSync(path.join(ROOT, 'static/js/client/musicshare.js'), 'utf8');
const NOSTR_JS = fs.readFileSync(path.join(ROOT, 'static/vendor/nostr/nostr.bundle.js'), 'utf8');
const APP_JS = clientSourceAt(path.join(ROOT, 'static/js/client/app.js'));

// nostr-tools runs in THIS realm: finalizeEvent checks `Array.isArray`-style shapes, and an array
// built in another vm context is not one of its arrays. The tags are copied across on publish.
globalThis.window = globalThis;
vm.runInThisContext(NOSTR_JS);
const NT = globalThis.NostrTools;
const hex = u8 => Buffer.from(u8).toString('hex');
const unhex = h => Uint8Array.from(Buffer.from(h, 'hex'));
const sha256hex = async u8 => hex(new Uint8Array(await webcrypto.subtle.digest('SHA-256', u8)));

/* ---- the network: one relay, one Blossom, shared by everyone -------------------------------- */
function makeNet(){
  const net = { events: [], blobs: new Map(), relayComplete: true, uploads: 0, published: [] };
  const dOf = e => ((e.tags || []).find(t => t[0] === 'd') || [])[1] || '';
  net.publish = (ev) => {
    net.published.push(ev);
    if(ev.kind === 5){
      for(const t of ev.tags){ if(t[0] !== 'a') continue;
        const parts = String(t[1]).split(':'), k = parts[0], pk = parts[1], d = parts.slice(2).join(':');
        net.events = net.events.filter(e => !(String(e.kind) === k && e.pubkey === pk && dOf(e) === d && e.created_at <= ev.created_at)); }
      net.events.push(ev); return true;
    }
    if(ev.kind >= 30000 && ev.kind < 40000){
      const cur = net.events.find(e => e.kind === ev.kind && e.pubkey === ev.pubkey && dOf(e) === dOf(ev));
      if(cur && cur.created_at > ev.created_at) return false;
      net.events = net.events.filter(e => e !== cur);
    }
    net.events.push(ev); return true;
  };
  const match = (f, e) => (!f.kinds || f.kinds.includes(e.kind)) && (!f.authors || f.authors.includes(e.pubkey))
    && Object.keys(f).filter(k => k[0] === '#').every(k => (e.tags || []).some(t => t[0] === k.slice(1) && f[k].includes(t[1])));
  net.query = (filters) => { const out = net.events.filter(e => filters.some(f => match(f, e))).map(e => JSON.parse(JSON.stringify(e)));
    Object.defineProperty(out, 'complete', { value: net.relayComplete, enumerable: false }); return out; };
  net.put = async (pk, bytes) => { const s = await sha256hex(bytes); net.uploads++;
    const b = net.blobs.get(s) || { bytes, owners: new Set() }; b.owners.add(pk); net.blobs.set(s, b); return s; };
  net.del = (pk, s) => { if(net.failDel && net.failDel.has(s)) return false; const b = net.blobs.get(s); if(!b) return true; b.owners.delete(pk); if(!b.owners.size) net.blobs.delete(s); return true; };
  return net;
}

/* ---- one person: a fresh vm context running the shipped module -------------------------------- */
function person(net, name){
  const sk = NT.generateSecretKey(), pk = NT.getPublicKey(sk);
  const store = [];
  const lib = new Map();          // sha → {m, plain}
  let clock = 1_700_000_000 + Math.floor(Math.random() * 1000);
  const mk = webcrypto.getRandomValues(new Uint8Array(32));
  const ls = {};
  const ctx = { console, crypto: webcrypto, TextEncoder, TextDecoder, Uint8Array, ArrayBuffer, Map, Set, Promise, JSON, Math,
    Date, Object, Array, String, Number, RegExp, Error, Blob, URL, atob, btoa, setTimeout, clearTimeout, location: { href: 'https://node.test/client' } };
  ctx.window = ctx; ctx.globalThis = ctx; ctx.self = ctx;
  ctx.localStorage = { getItem: k => (k in ls ? ls[k] : null), setItem: (k, v) => { ls[k] = String(v); }, removeItem: k => { delete ls[k]; } };
  ctx.File = class extends Blob { constructor(parts, n, o){ super(parts, o); this.name = n; } };
  ctx.NostrTools = NT;
  ctx.fetch = async (url, opts = {}) => {
    const m = String(url).match(/\/blossom\/([0-9a-f]{64})/);
    const b = m && net.blobs.get(m[1]);
    if((opts.method || 'GET') === 'HEAD') return { ok: !!b, status: b ? 200 : 404 };
    if(!b) return { ok: false, status: 404 };
    return { ok: true, status: 200, arrayBuffer: async () => b.bytes.slice().buffer };
  };
  ctx.Store = { query: () => store.slice(), saveEvent: e => { if(!store.some(x => x.id === e.id)) store.push(e); } };
  ctx.Relay = { ready: async () => {}, query: async (f) => net.query(f), publish: async ev => ({ ok: net.publish(ev) }) };
  const conv = peer => NT.nip44.getConversationKey(sk, peer);
  ctx.__PC = {
    me: () => ({ pubkey: pk }),
    nip44enc: async (peer, pt) => NT.nip44.encrypt(pt, conv(peer)),
    nip44dec: async (peer, ct) => NT.nip44.decrypt(ct, conv(peer)),
    publish: async (kind, content, tags) => {
      // A relay answers in its own time. Jitter, so two unserialized writers WOULD interleave.
      if(net.jitter) await new Promise(r => setTimeout(r, (net.calls = (net.calls || 0) + 1) % 2 ? 25 : 0));
      clock = Math.max(clock + 1, Math.floor(Date.now() / 1000));
      const ev = NT.finalizeEvent({ kind, content: String(content), tags: JSON.parse(JSON.stringify(tags)), created_at: clock }, sk);   // cross-realm arrays
      return { ok: net.publish(ev), ev };
    },
    mediaServer: () => 'https://node.test/blossom',
    uploadBlob: async (file) => 'https://node.test/blossom/' + await net.put(pk, new Uint8Array(await file.arrayBuffer())),
    releaseBlob: async s => net.del(pk, s),
    musicShareKey: async sha => ctx.PCMusicShare.deriveKey(mk, sha),
    musicPlainOf: async sha => { const t = lib.get(sha); if(!t) throw new Error('no such track'); return t.plain; },
    musicLibrary: () => [...lib.keys()],
    musicLibraryAdd: async entries => { for(const [s, m] of entries) lib.set(s, { m }); return true; },
    toast: () => {}, enc: s => String(s),
  };
  vm.createContext(installStateGlobals(ctx) && ctx);
  vm.runInContext(SHARE_JS, ctx, { filename: 'musicshare.js' });
  return { name, sk, pk, lib, ctx, S: ctx.PCMusicShare, store, mk, ls,
           addTrack: async (plain, name) => { const s = await sha256hex(webcrypto.getRandomValues(new Uint8Array(16)));
             lib.set(s, { m: { name, mime: 'audio/wav', size: plain.length, enc: true, mk: true, folder: 'Music' }, plain }); return s; } };
}

/* app.js's own decrypt, lifted by name so the test reads the function the player calls. */
function shippedDriveDecrypt(p){
  const grab = (sig) => { const at = APP_JS.indexOf(sig); if(at < 0) throw new Error('moved: ' + sig);
    let i = APP_JS.indexOf('{', at), depth = 0;
    for(; i < APP_JS.length; i++){ if(APP_JS[i] === '{') depth++; else if(APP_JS[i] === '}' && --depth === 0) break; }
    return APP_JS.slice(at, i + 1); };
  const src = [grab('function _b64u8(b)'), grab('async function _aesDecrypt(ct,key,iv)'), grab('async function _driveDecrypt(m, bytes, indexed)')].join('\n');
  const ctx = { crypto: webcrypto, atob, Uint8Array, JSON, ME: { pubkey: p.pk },
    signer: { nip44dec: (peer, ct) => p.ctx.__PC.nip44dec(peer, ct) },
    FilesIdx: { _ensureMK: async () => { throw new Error('a shared track must never need the master key'); } } };
  vm.createContext(installStateGlobals(ctx) && ctx);
  vm.runInContext(src + '\nthis._driveDecrypt=_driveDecrypt;', ctx);
  return ctx._driveDecrypt;
}

const wav = (n, seed) => { const u = new Uint8Array(n); for(let i = 0; i < n; i++) u[i] = (i * 31 + seed) & 255; return u; };
const eq = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
const out = {};
async function run(name, fn){ try{ const d = await fn(); out[name] = { ok: d === true || (d && d.ok !== false && !d.fail), detail: d }; }
                              catch(e){ out[name] = { ok: false, detail: String(e && e.stack || e) }; } }

await run('derived key is per track, deterministic, and not the master key', async () => {
  const p = person(makeNet(), 'a'); const s1 = 'a'.repeat(64), s2 = 'b'.repeat(64);
  const k1 = await p.S.deriveKey(p.mk, s1), k1b = await p.S.deriveKey(p.mk, s1), k2 = await p.S.deriveKey(p.mk, s2);
  return { ok: k1.length === 32 && eq(k1, k1b) && !eq(k1, k2) && !eq(k1, p.mk)
           && (await p.S.deriveKey(new Uint8Array(5), s1)) === null && (await p.S.deriveKey(p.mk, 'nope')) === null };
});

await run('wire validation refuses junk', async () => {
  const p = person(makeNet(), 'a'), S = p.S;
  const k = S.b64(new Uint8Array(32)), iv = S.b64(new Uint8Array(12));
  const good = S.cleanItem({ s: 'c'.repeat(64), k, iv, n: 'x', m: 'audio/ogg', z: 5, e: 'ogg' });
  return { ok: !!good && !S.cleanItem({ s: 'zz', k, iv }) && !S.cleanItem({ s: 'c'.repeat(64), k: S.b64(new Uint8Array(31)), iv })
           && !S.cleanItem({ s: 'c'.repeat(64), k, iv: S.b64(new Uint8Array(11)) })
           && S.cleanItem({ s: 'c'.repeat(64), k, iv, m: 'text/html' }).m === 'audio/mpeg'
           && S.cleanBody({ v: 1, from: 'x', tracks: [] }, 'y') === null
           && S.parseD(S.dTag('abcd1234', 'f'.repeat(64))).r16 === 'f'.repeat(16) && S.parseD('pcai:musicshare:x') === null };
});

await run('A shares; B plays it; B adds it; A stops sharing; B keeps it; C never could', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B'), C = person(net, 'C');
  const p1 = wav(3000, 1), p2 = wav(5000, 2);
  const s1 = await A.addTrack(p1, 'One'), s2 = await A.addTrack(p2, 'Two');
  const tracks = [s1, s2].map(sha => ({ sha, name: A.lib.get(sha).m.name, mime: 'audio/wav', size: A.lib.get(sha).plain.length, ext: 'wav' }));
  const r = await A.S.share({ name: 'Road trip', tracks, to: [B.pk] });
  if(!r.ok || r.sent.length !== 1) return { fail: 'share', r };
  const doc = net.events.find(e => e.kind === 30078 && e.pubkey === A.pk);
  const tagsOk = doc.tags.some(t => t[0] === 'p' && t[1] === B.pk) && doc.tags.some(t => t[0] === 'l' && t[1] === 'pcai-musicshare')
              && ((doc.tags.find(t => t[0] === 'd') || [])[1] || '').endsWith(':' + B.pk.slice(0, 16));
  // Not a word of the list is readable on the wire.
  const leaks = doc.content.includes('Road trip') || doc.content.includes('One');
  // B sees it, and can PLAY it (decrypt the bytes).
  const ins = await B.S.loadIn();
  if(ins.length !== 1) return { fail: 'B sees nothing', ins };
  const bt = await B.S.tracksOf(ins[0]); B.S.register(ins[0], bt);
  const played = await B.S.plain(bt[0].s);
  // B adds both to the library.
  const add = await B.S.addToLibrary(ins[0], bt);
  const entry = B.lib.get(bt[0].s).m;
  const noRawKey = !JSON.stringify(entry).includes(bt[0].k);
  // …and the SHIPPED player decrypt opens the record B wrote, with B's key and nothing else.
  const dec = shippedDriveDecrypt(B);
  const beforeRevoke = await dec(entry, net.blobs.get(bt[0].s).bytes, true);
  // C: finds nothing addressed to them, and cannot open A's document to B even when handed it.
  const cIn = await C.S.loadIn();
  let cDecrypted = true; try{ await C.ctx.__PC.nip44dec(A.pk, doc.content); }catch(_){ cDecrypted = false; }
  let cBody = true; try{ JSON.parse(await C.ctx.__PC.nip44dec(A.pk, doc.content)); }catch(_){ cBody = false; }
  // A stops sharing.
  await A.S.loadOut();
  const rv = await A.S.revoke(r.id);
  const k5 = net.published.findIndex(e => e.kind === 5 && e.pubkey === A.pk);
  const tomb = net.published.findIndex((e, i) => i > k5 && e.kind === 30078 && e.pubkey === A.pk && e.content === '');
  const bAfter = await B.S.loadIn();
  // B's copy survives A's release: B owns the bytes too.
  const blob = net.blobs.get(bt[0].s);
  const after = blob ? await dec(entry, blob.bytes, true) : null;
  return { ok: tagsOk && !leaks && eq(played, p1) && add.ok && add.added === 2 && noRawKey && eq(beforeRevoke, p1)
             && cIn.length === 0 && !cDecrypted && !cBody && rv.ok && k5 >= 0 && tomb > k5 && bAfter.length === 0
             && !!blob && !blob.owners.has(A.pk) && blob.owners.has(B.pk) && after && eq(after, p1) && rv.released >= 1,
           tagsOk, leaks, add, noRawKey, cIn: cIn.length, cDecrypted, rv, k5, tomb, bAfter: bAfter.length, owners: blob && [...blob.owners].length };
});

await run('the same song shared twice is ONE blob', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B'), D = person(net, 'D');
  const s = await A.addTrack(wav(4000, 9), 'Same');
  const t = [{ sha: s, name: 'Same', mime: 'audio/wav', size: 4000, ext: 'wav' }];
  const r1 = await A.S.share({ name: 'one', tracks: t, to: [B.pk] });
  const n1 = net.blobs.size;
  delete A.ls[Object.keys(A.ls)[0]];                     // even with the device cache gone
  const r2 = await A.S.share({ name: 'two', tracks: t, to: [D.pk] });
  const docs = net.events.filter(e => e.kind === 30078 && e.pubkey === A.pk);
  const i1 = JSON.parse(await B.ctx.__PC.nip44dec(A.pk, docs.find(e => e.tags.some(x => x[1] === B.pk)).content)).tracks[0].s;
  const i2 = JSON.parse(await D.ctx.__PC.nip44dec(A.pk, docs.find(e => e.tags.some(x => x[1] === D.pk)).content)).tracks[0].s;
  return { ok: r1.ok && r2.ok && i1 === i2 && net.blobs.size === n1, i1, i2, n1, n2: net.blobs.size };
});

await run('revoking one of two shares keeps the copy the other still uses', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B'), D = person(net, 'D');
  const s = await A.addTrack(wav(4000, 3), 'Kept');
  const t = [{ sha: s, name: 'Kept', mime: 'audio/wav', size: 4000, ext: 'wav' }];
  const r1 = await A.S.share({ name: 'one', tracks: t, to: [B.pk] });
  await A.S.share({ name: 'two', tracks: t, to: [D.pk] });
  await A.S.loadOut();
  const rv = await A.S.revoke(r1.id);
  const dIn = await D.S.loadIn(); const dt = await D.S.tracksOf(dIn[0]); D.S.register(dIn[0], dt);
  const still = await D.S.plain(dt[0].s);
  return { ok: rv.ok && rv.released === 0 && still.length === 4000, rv };
});

await run('an incomplete read never releases anything', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B');
  const s = await A.addTrack(wav(2000, 4), 'x');
  const r = await A.S.share({ name: 'n', tracks: [{ sha: s, name: 'x', mime: 'audio/wav', size: 2000, ext: 'wav' }], to: [B.pk] });
  net.relayComplete = false;
  await A.S.loadOut();
  const refs = await A.S.refIds();
  const rv = await A.S.revoke(r.id);
  const sAddr = [...net.blobs.keys()].find(k => [...net.blobs.get(k).owners].includes(A.pk) && k !== s);
  return { ok: refs === null && rv.released === 0 && !!sAddr, refs, rv };
});

await run('refIds names every copy a live share uses', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B');
  const s = await A.addTrack(wav(2000, 5), 'x');
  await A.S.share({ name: 'n', tracks: [{ sha: s, name: 'x', mime: 'audio/wav', size: 2000, ext: 'wav' }], to: [B.pk] });
  const bIn = await B.S.loadIn(); const bt = await B.S.tracksOf(bIn[0]);
  const refs = await A.S.refIds();
  return { ok: !!refs && refs.has(bt[0].s), n: refs && refs.size };
});

await run('a share too big for one event carries its list in a sealed blob', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B');
  const tracks = [];
  for(let i = 0; i < 330; i++){ const s = await A.addTrack(wav(64, i), 'Track number ' + i + ' ' + 'x'.repeat(40));
    tracks.push({ sha: s, name: A.lib.get(s).m.name, mime: 'audio/wav', size: 64, ext: 'wav' }); }
  const r = await A.S.share({ name: 'Huge', tracks, to: [B.pk] });
  const doc = net.events.find(e => e.kind === 30078 && e.pubkey === A.pk);
  const body = JSON.parse(await B.ctx.__PC.nip44dec(A.pk, doc.content));
  const ins = await B.S.loadIn(); const bt = await B.S.tracksOf(ins[0]);
  B.S.register(ins[0], bt);
  const last = await B.S.plain(bt[329].s);
  return { ok: r.ok && !!body.tl && body.tracks.length === 0 && bt.length === 330 && eq(last, wav(64, 329)), tl: !!body.tl, n: bt.length };
});

await run('a forged body naming another author is ignored', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B'), M = person(net, 'M');
  // M publishes a share to B whose body claims to be FROM A.
  const body = { v: 1, id: 'forged01', name: 'Totally A', from: A.pk, srv: 'https://node.test/blossom', tracks: [] };
  await M.ctx.__PC.publish(30078, await M.ctx.__PC.nip44enc(B.pk, JSON.stringify(body)),
    [['d', M.S.dTag('forged01', B.pk)], ['p', B.pk], ['l', 'pcai-musicshare']]);
  const ins = await B.S.loadIn();
  return { ok: ins.length === 0, n: ins.length };
});

await run('a document addressed to somebody else is not a share with me', async () => {
  const net = makeNet(); const B = person(net, 'B'), D = person(net, 'D'), M = person(net, 'M');
  // p-tag says B, but the address (d) belongs to D: not a share TO B, whatever the p-tag claims.
  const body = { v: 1, id: 'misaddr1', name: 'Not yours', from: M.pk, srv: 'https://node.test/blossom', tracks: [] };
  await M.ctx.__PC.publish(30078, await M.ctx.__PC.nip44enc(B.pk, JSON.stringify(body)),
    [['d', M.S.dTag('misaddr1', D.pk)], ['p', B.pk], ['l', 'pcai-musicshare']]);
  const ins = await B.S.loadIn();
  return { ok: ins.length === 0, n: ins.length };
});

await run('writes are serialized', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B'), D = person(net, 'D');
  const s = await A.addTrack(wav(1000, 6), 'x');
  const t = [{ sha: s, name: 'x', mime: 'audio/wav', size: 1000, ext: 'wav' }];
  net.jitter = true;
  const [r1, r2] = await Promise.all([A.S.share({ name: 'p', tracks: t, to: [B.pk, D.pk] }), A.S.share({ name: 'q', tracks: t, to: [B.pk, D.pk] })]);
  const order = net.published.filter(e => e.kind === 30078).map(e => (e.tags.find(x => x[0] === 'd') || [])[1].split(':')[2]);
  // Each share's documents go out together, never interleaved.
  return { ok: r1.ok && r2.ok && order[0] === order[1] && order[2] === order[3] && order[0] !== order[2], order };
});

await run('adding twice does not duplicate, and uploads nothing the second time', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B');
  const s = await A.addTrack(wav(1500, 7), 'x');
  await A.S.share({ name: 'p', tracks: [{ sha: s, name: 'x', mime: 'audio/wav', size: 1500, ext: 'wav' }], to: [B.pk] });
  const ins = await B.S.loadIn(); const bt = await B.S.tracksOf(ins[0]);
  const a1 = await B.S.addToLibrary(ins[0], bt); const up = net.uploads;
  const a2 = await B.S.addToLibrary(ins[0], bt);
  return { ok: a1.added === 1 && a2.added === 0 && a2.skipped === 1 && net.uploads === up && B.lib.size === 1, a1, a2 };
});

/* ---- UNSHARING CLEANS UP, EVENTUALLY AND SAFELY -------------------------------------------------
 * A share's copies are the sharer's blobs. Stopping the share must release them -- but only after a
 * COMPLETE read proves nothing live still points at them, and a release that could not happen then
 * must happen LATER, not never: the documents are tombstoned by then, so "which copies" is only
 * known if it was written down before. */
const ownedBy = (net, pk) => [...net.blobs.entries()].filter(([, b]) => b.owners.has(pk)).map(([s]) => s);
const shareOne = async (net, A, B, n, seed) => {
  const s = await A.addTrack(wav(n, seed), 'song' + seed);
  const r = await A.S.share({ name: 'p' + seed, tracks: [{ sha: s, name: 'song' + seed, mime: 'audio/wav', size: n, ext: 'wav' }], to: [B.pk] });
  const bIn = await B.S.loadIn(); const bt = await B.S.tracksOf(bIn.find(x => x.id === r.id) || bIn[0]);
  return { r, copy: bt[0].s, bIn, bt };
};

await run('a release skipped by an incomplete read happens on the next complete one', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B');
  const { r, copy, bIn, bt } = await shareOne(net, A, B, 2500, 11);
  await B.S.addToLibrary(bIn[0], bt);                          // B keeps it
  net.relayComplete = false; await A.S.loadOut();
  const rv = await A.S.revoke(r.id);
  const heldAfterRevoke = net.blobs.get(copy).owners.has(A.pk);
  net.relayComplete = true; await A.S.loadOut(); await A.S.drainPending();
  const b = net.blobs.get(copy);
  return { ok: rv.ok && rv.released === 0 && heldAfterRevoke && !!b && !b.owners.has(A.pk) && b.owners.has(B.pk)
             && A.S.pendingReleases().length === 0,
           rv, heldAfterRevoke, owners: b && [...b.owners].length, pending: A.S.pendingReleases() };
});

await run('stopping with one of two people keeps the copy; the last one releases it', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B'), D = person(net, 'D');
  const s = await A.addTrack(wav(1800, 12), 'x');
  const r = await A.S.share({ name: 'two', tracks: [{ sha: s, name: 'x', mime: 'audio/wav', size: 1800, ext: 'wav' }], to: [B.pk, D.pk] });
  await A.S.loadOut();
  const r1 = await A.S.revoke(r.id, [B.pk]);
  const dIn = await D.S.loadIn(); const dt = await D.S.tracksOf(dIn[0]); D.S.register(dIn[0], dt);
  const dStill = (await D.S.plain(dt[0].s)).length;
  const bSees = (await B.S.loadIn()).length;
  const r2 = await A.S.revoke(r.id, [D.pk]);
  return { ok: r1.ok && r1.released === 0 && dStill === 1800 && bSees === 0 && r2.ok && r2.released === 1
             && !net.blobs.has(dt[0].s), r1, r2, dStill, bSees };
});

await run('a release that fails is retried, not forgotten', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B');
  const { r, copy } = await shareOne(net, A, B, 1200, 13);
  await A.S.loadOut();
  net.failDel = new Set([copy]);
  const rv = await A.S.revoke(r.id);
  const pendingAfterFail = A.S.pendingReleases().slice();
  net.failDel = null;
  await A.S.loadOut(); await A.S.drainPending();
  return { ok: rv.ok && rv.released === 0 && pendingAfterFail.includes(copy) && !net.blobs.has(copy)
             && A.S.pendingReleases().length === 0, rv, pendingAfterFail };
});

await run("a big share's sealed song list and its songs are all released", async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B');
  const tracks = [];
  for(let i = 0; i < 330; i++){ const s = await A.addTrack(wav(48, 100 + i), 'Big ' + i + ' ' + 'y'.repeat(40));
    tracks.push({ sha: s, name: A.lib.get(s).m.name, mime: 'audio/wav', size: 48, ext: 'wav' }); }
  const r = await A.S.share({ name: 'Huge', tracks, to: [B.pk] });
  const before = ownedBy(net, A.pk).length;
  await A.S.loadOut();
  const rv = await A.S.revoke(r.id);
  return { ok: rv.ok && before === 331 && ownedBy(net, A.pk).length === 0 && rv.released === 331, before, left: ownedBy(net, A.pk).length, rv };
});

await run('sharing again after stopping works and plays', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B');
  const s = await A.addTrack(wav(900, 14), 'again');
  const t = [{ sha: s, name: 'again', mime: 'audio/wav', size: 900, ext: 'wav' }];
  const r1 = await A.S.share({ name: 'first', tracks: t, to: [B.pk] });
  await A.S.loadOut(); await A.S.revoke(r1.id);
  const r2 = await A.S.share({ name: 'second', tracks: t, to: [B.pk] });
  const bIn = await B.S.loadIn(); const bt = await B.S.tracksOf(bIn[0]); B.S.register(bIn[0], bt);
  const played = await B.S.plain(bt[0].s);
  return { ok: r2.ok && bIn.length === 1 && eq(played, wav(900, 14)), r2, n: bIn.length };
});

await run('a copy shared again before the retry is not released', async () => {
  const net = makeNet(); const A = person(net, 'A'), B = person(net, 'B'), D = person(net, 'D');
  const s = await A.addTrack(wav(700, 15), 'back');
  const t = [{ sha: s, name: 'back', mime: 'audio/wav', size: 700, ext: 'wav' }];
  const r1 = await A.S.share({ name: 'first', tracks: t, to: [B.pk] });
  net.relayComplete = false; await A.S.loadOut(); await A.S.revoke(r1.id);   // release deferred
  net.relayComplete = true;
  await A.S.share({ name: 'again', tracks: t, to: [D.pk] });                  // same copy, live again
  await A.S.loadOut(); await A.S.drainPending();
  const dIn = await D.S.loadIn(); const dt = await D.S.tracksOf(dIn[0]); D.S.register(dIn[0], dt);
  let plays = false; try{ plays = eq(await D.S.plain(dt[0].s), wav(700, 15)); }catch(_){ plays = false; }
  return { ok: plays && A.S.pendingReleases().length === 0, plays, pending: A.S.pendingReleases() };
});

process.stdout.write(JSON.stringify(out));
