/* The account's read markers (static/js/client/seen.js, `d=pcai:seen`) -- the SHIPPED engine against a
 * fake relay that answers, refuses, times out and hands back stale copies.
 *
 *   two devices      A marks, B (fresh storage, same relay) inherits it and publishes nothing
 *   max merge        an OLDER copy arriving later moves nothing backwards
 *   could not ask    a failed / incomplete read never publishes; the mark waits, then MERGES into what
 *                    the relay holds (another device's marks survive)
 *   unreadable       a copy we cannot decrypt is never written over
 *   per account      account B never sees, nor publishes, account A's marks; a signed-out engine is inert
 *   bounded          the conversation map is capped, marks covered by dmAll are dropped, no future dates
 */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../../static/js/client/seen.js', import.meta.url), 'utf8');
const ctx = { setTimeout, clearTimeout, console, JSON, Math, Date, Object, Number, Promise, Set, Array, String, Error };
vm.createContext(ctx); vm.runInContext(src, ctx);
const PCSeen = ctx.PCSeen;
let failed = 0;
const ok = (c, m) => { if (!c) { failed++; console.error('FAIL: ' + m); } };
const A = 'a'.repeat(64), B = 'b'.repeat(64), P = 'c'.repeat(64), Q = 'd'.repeat(64);
const tick = ms => new Promise(r => setTimeout(r, ms));

function relay() {
  const r = { events: [], mode: 'ok', writable: true, published: [], seq: 0 };
  r.query = async filters => {
    if (r.mode === 'throw') throw new Error('down');
    const f = filters[0];
    const out = r.mode === 'timeout' ? [] : r.events.filter(e => f.authors.includes(e.pubkey) && e.kind === 30078 &&
      e.tags.some(t => t[0] === 'd' && f['#d'].includes(t[1])));
    Object.defineProperty(out, 'complete', { value: r.mode !== 'timeout' });
    return out;
  };
  r.put = (owner, doc, at) => r.events.push({ id: 'e' + (++r.seq), pubkey: owner, kind: 30078, created_at: at || 1000 + r.seq,
    tags: [['d', 'pcai:seen']], content: 'enc:' + owner + ':' + JSON.stringify(doc) });
  return r;
}
function storage() { const m = new Map(); return { m, get: k => (m.has(k) ? m.get(k) : null), set: (k, v) => m.set(k, String(v)) }; }
function engine(r, owner, store, extra = {}) {
  let me = owner;
  const e = PCSeen.create({
    owner, storage: store, delay: 5, retry: 20,
    query: r.query,
    decrypt: async ct => { const m = /^enc:([0-9a-f]{64}):(.*)$/s.exec(ct); if (!m || m[1] !== owner) throw new Error('not for us'); return m[2]; },
    encrypt: async text => 'enc:' + owner + ':' + text,
    publish: async (kind, content, tags) => {
      if (!r.writable) return { ok: false };   // writes can land while reads cannot -- the case that wipes
      r.published.push({ owner, kind, content, tags });
      r.events.push({ id: 'e' + (++r.seq), pubkey: owner, kind, created_at: 2000 + r.seq, tags, content });
      return { ok: true };
    },
    isCurrent: o => o === me,
    ...extra,
  });
  e.signOut = () => { me = ''; };
  return e;
}
const docOf = p => JSON.parse(p.content.replace(/^enc:[0-9a-f]{64}:/, ''));

// ---- two devices ----
{
  const r = relay();
  const a = engine(r, A, storage());
  await a.load();
  ok(a.answered(), 'an empty, complete answer is an answer');
  a.markNotif(500); a.markDm(P, 300); a.markDmAll(200);
  await a.flush();
  ok(r.published.length === 1, 'device A publishes one document, got ' + r.published.length);
  const d = docOf(r.published[0]);
  ok(d.v === 1 && d.notif === 500 && d.dmAll === 200 && d.dm[P] === 300, 'the document carries the marks: ' + JSON.stringify(d));
  ok(r.published[0].tags.length === 1 && r.published[0].tags[0][1] === 'pcai:seen', 'only the d tag');

  let changes = 0;
  const b = engine(r, A, storage(), { onChange: () => changes++ });
  ok(b.notif() === 0, 'a fresh device starts at nothing');
  await b.load();
  ok(b.notif() === 500 && b.dmFor(P) === 300 && b.dmFor(Q) === 200, 'device B inherits A: ' + JSON.stringify(b.snapshot()));
  ok(changes >= 1, 'device B was told its counts changed');
  await b.flush(); await tick(30);
  ok(r.published.length === 1, 'device B had nothing new to say and wrote nothing');
  ok(b.markNotif(501) === true && b.markNotif(400) === false, 'a mark moves only forward');

  // ---- an older copy arriving later moves nothing back ----
  const before = JSON.stringify(b.snapshot());
  await b.ingest({ pubkey: A, kind: 30078, tags: [['d', 'pcai:seen']], content: 'enc:' + A + ':' + JSON.stringify({ v: 1, notif: 10, dmAll: 1, dm: { [P]: 5 } }) });
  ok(b.snapshot().notif === 501 && b.dmFor(P) === 300, 'an older copy un-read things: ' + JSON.stringify(b.snapshot()) + ' was ' + before);
  // …and a newer one (another device) moves it forward, live.
  await b.ingest({ pubkey: A, kind: 30078, tags: [['d', 'pcai:seen']], content: 'enc:' + A + ':' + JSON.stringify({ v: 1, notif: 900, dm: { [Q]: 950 } }) });
  ok(b.notif() === 900 && b.dmFor(Q) === 950, 'a newer copy is adopted live');
  // a copy someone ELSE signed is nobody's business
  await b.ingest({ pubkey: B, kind: 30078, tags: [['d', 'pcai:seen']], content: 'enc:' + B + ':' + JSON.stringify({ notif: 99999 }) });
  ok(b.notif() === 900, 'a stranger\'s document moved the marks');
  b.stop(); a.stop();
}

// ---- could not ask: never publish, then merge into what the relay holds ----
for (const mode of ['throw', 'timeout']) {
  const r = relay();
  r.put(A, { v: 1, notif: 50, dmAll: 0, dm: { [Q]: 70 } });             // another device's marks
  r.mode = mode;
  const st = storage();
  const a = engine(r, A, st);
  await a.load();
  ok(!a.answered(), mode + ': an unanswered read counts as an answer');
  a.markNotif(40); a.markDm(P, 30);
  await a.flush(); await tick(15);
  ok(r.published.length === 0, mode + ': published over an unread document (' + r.published.length + ')');
  ok(a.pending(), mode + ': the mark was dropped instead of kept pending');
  ok(JSON.parse(st.get('pc_seen:' + A)).pending === true, mode + ': pending is not remembered across a reload');
  r.mode = 'ok';
  await tick(120);                                                       // the retry
  await a.flush();
  ok(r.published.length === 1, mode + ': the pending mark never went out once the relay answered');
  const d = r.published.length ? docOf(r.published[r.published.length - 1]) : {};
  ok(d.notif === 50 && d.dm && d.dm[Q] === 70 && d.dm[P] === 30, mode + ': the write did not keep the relay\'s marks: ' + JSON.stringify(d));
  a.stop();
}

// ---- answered once, then the relay goes away: the re-read before a write must answer too ----
{
  const r = relay();
  const a = engine(r, A, storage());
  await a.load();
  ok(a.answered(), 'the first read answered');
  r.put(A, { v: 1, notif: 800, dm: { [Q]: 810 } });                     // another device writes meanwhile
  r.mode = 'timeout';
  a.markNotif(5); await a.flush(); await tick(10);
  ok(r.published.length === 0, 'published on the strength of an unanswered re-read');
  r.mode = 'ok'; await tick(60); await a.flush();
  const d = r.published.length ? docOf(r.published[r.published.length - 1]) : null;
  ok(!d || (d.notif === 800 && d.dm[Q] === 810), 'a later write dropped the other device\'s marks: ' + JSON.stringify(d));
  a.stop();
}

// ---- a pending mark survives the page dying ----
{
  const r = relay(), st = storage();
  r.mode = 'throw';
  const a = engine(r, A, st); a.markNotif(77); a.stop();
  r.mode = 'ok';
  const a2 = engine(r, A, st);
  ok(a2.notif() === 77 && a2.pending(), 'the cached copy lost the pending mark');
  await a2.load(); await tick(30); await a2.flush();
  ok(r.published.length === 1 && docOf(r.published[0]).notif === 77, 'the mark from the dead page was not published');
  a2.stop();
}

// ---- unreadable: never overwrite ----
{
  const r = relay();
  r.events.push({ id: 'x', pubkey: A, kind: 30078, created_at: 5000, tags: [['d', 'pcai:seen']], content: 'garbage-we-cannot-open' });
  const a = engine(r, A, storage());
  await a.load(); a.markNotif(10); await a.flush(); await tick(15);
  ok(!a.answered() && r.published.length === 0, 'wrote over a document it could not read');
  a.stop();
}

// ---- per account ----
{
  const r = relay(), st = storage();
  const a = engine(r, A, st);
  await a.load(); a.markDm(P, 123); a.markNotif(456); await a.flush();
  const b = engine(r, B, st);                                            // switch account, same device
  const forged = storage(); forged.set('pc_seen:' + B, JSON.stringify({ owner: A, notif: 777, dm: { [P]: 777 } }));
  ok(engine(r, B, forged).notif() === 0, 'a cache entry recorded for another account was adopted');
  ok(b.notif() === 0 && b.dmFor(P) === 0, 'account B inherited account A\'s marks: ' + JSON.stringify(b.snapshot()));
  await b.load();
  ok(b.notif() === 0 && b.dmFor(P) === 0, 'account B adopted account A\'s document');
  b.markNotif(9); await b.flush();
  const mine = r.published.filter(p => p.owner === B);
  ok(mine.length === 1 && !mine[0].content.includes(P) && docOf(mine[0]).notif === 9, 'account B published A\'s marks');
  a.signOut();
  ok(a.markNotif(99999) === false, 'a signed-out engine still marks');
  const n = r.published.length; await a.flush(); await tick(15);
  ok(r.published.length === n, 'a signed-out engine still publishes');
  a.stop(); b.stop();
}

// ---- bounded ----
{
  const r = relay();
  const a = engine(r, A, storage(), { now: () => 10000 });
  await a.load();
  for (let i = 0; i < 700; i++) a.markDm(i.toString(16).padStart(64, '0'), 1000 + i);
  ok(Object.keys(a.snapshot().dm).length === PCSeen.CAP, 'the conversation map is not capped: ' + Object.keys(a.snapshot().dm).length);
  ok(a.dmFor((699).toString(16).padStart(64, '0')) === 1699, 'the cap dropped the NEWEST marks');
  a.markDmAll(1500);
  ok(Object.values(a.snapshot().dm).every(v => v > 1500), 'marks covered by dmAll are kept');
  a.markNotif(10 ** 9);
  ok(a.notif() <= 10000, 'a date from the future was marked read: ' + a.notif());
  ok(a.markDm('not-a-key', 5) === false, 'a junk conversation key was accepted');
  a.stop();
}

if (failed) { console.error(failed + ' failed'); process.exit(1); }
console.log('seen sync runtime ok');
