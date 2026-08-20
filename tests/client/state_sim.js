/* The per-file engine (syncstate.js), proven offline before it may touch a file.
 *
 * Every scenario here is either a cell of the decision table or a named failure from the two
 * engines this one replaces — the dirty join that minted a conflict per path, the silent revert
 * from a journal read as stale, the re-add ghosts, the mass-trash orders. The sim runs the SHIPPED
 * file under node; if it passes here and fails on a device, the executor or the transport is lying
 * to it, never the table.
 */
'use strict';
const path = require('path');
const ROOT = path.resolve(__dirname, '../../static/js/client');
const S = require(path.join(ROOT, 'syncstate.js'));
const P = require(path.join(ROOT, 'foldersync.js'));

let failures = 0, checks = 0;
function ok(cond, name){
  checks++;
  if(!cond){ failures++; console.log('FAIL  ' + name); }
}
function eq(a, b, name){ ok(JSON.stringify(a) === JSON.stringify(b), name + ' — got ' + JSON.stringify(a)); }

const F = (v, csum, extra) => Object.assign({ v, by: 'other', size: 10, mtime: 1000, csum,
                                              sha: 'blob-' + csum }, extra || {});
const T = (v, extra) => Object.assign({ v, by: 'other', deletedAt: 5000 }, extra || {});
const D = (csum, extra) => Object.assign({ size: 10, mtime: 1000, csum }, extra || {});
const J = (v, csum, extra) => Object.assign({ v, by: 'me', size: 10, mtime: 1000, csum,
                                              sha: 'blob-' + csum,
                                              local: { size: 10, mtime: 1000, csum } }, extra || {});

const plan = (state, disk, index, o) =>
  S.plan(Object.assign({ state, disk, index, device: 'me', now: 9000 }, o || {}));
const only = (p, kind) => {
  for(const k of ['fetch','send','remove','tombstone','keepBoth','settle'])
    if(k !== kind) ok(p[k].length === 0, 'nothing unexpected in ' + k + ' (wanted only ' + kind + ')');
  return p[kind];
};

/* ---- the decision table, one cell at a time -------------------------------------------------- */

{ // Nothing anywhere: empty plan.
  const p = plan({}, {}, {});
  ok(p.unchanged === 0 && p.fetch.length + p.send.length + p.remove.length === 0, 'empty world, empty plan');
}
{ // New here: local file, no record, no journal → send.
  const p = plan({}, { a: D('x') }, {});
  const s = only(p, 'send');
  ok(s.length === 1 && s[0].path === 'a' && s[0].v === 1 && s[0].why === 'new here', 'new here → send v1');
}
{ // New elsewhere: record, nothing local, no journal → fetch.
  const p = plan({ a: F(1, 'x') }, {}, {});
  const f = only(p, 'fetch');
  ok(f.length === 1 && f[0].path === 'a' && f[0].why === 'new elsewhere', 'new elsewhere → fetch');
}
{ // In step: record applied, disk unchanged → unchanged, nothing planned.
  const p = plan({ a: F(1, 'x') }, { a: D('x') }, { a: J(1, 'x') });
  ok(p.unchanged === 1, 'in step → unchanged');
  only(p, '__none__');
}
{ // Changed elsewhere: record ahead, disk unchanged → fetch.
  const p = plan({ a: F(2, 'y') }, { a: D('x') }, { a: J(1, 'x') });
  const f = only(p, 'fetch');
  ok(f.length === 1 && f[0].why === 'changed elsewhere' && f[0].entry.csum === 'y', 'changed elsewhere → fetch');
}
{ // Changed here: disk differs from journal, record where we left it → send at bumped version.
  const p = plan({ a: F(3, 'x') }, { a: D('z') }, { a: J(3, 'x') });
  const s = only(p, 'send');
  ok(s.length === 1 && s[0].v === 4 && s[0].why === 'changed here', 'changed here → send v+1');
}
{ /* Deleted elsewhere: tombstone ahead, disk unchanged → REMOVE. Not "move to .pc-trash": the
   * trash is one place now, on the server, and a second per-device copy of the same idea is what
   * people actually experienced as the failure — a phone with 109 files in it, a tablet with 226,
   * and no single list anywhere that answered "what did I delete". Safe because the executor
   * confirms the store holds the bytes before it calls this. */
  const p = plan({ a: T(2) }, { a: D('x') }, { a: J(1, 'x') });
  const t = only(p, 'remove');
  ok(t.length === 1, 'deleted elsewhere → remove');
  ok(t[0].to === undefined, 'a deletion still names a destination — there is no local trash now');
  ok(!!t[0].entry, 'the record travels with the deletion, or the executor cannot check the store');
}
{ // Deleted here: journal knows it, disk lost it, record unchanged → tombstone at bumped version.
  const p = plan({ a: F(2, 'x') }, {}, { a: J(2, 'x') });
  const t = only(p, 'tombstone');
  ok(t.length === 1 && t[0].v === 3, 'deleted here → tombstone v+1');
}
{ // Deleted on both: tombstone ahead, nothing local → settle, and it counts as settledGone next sweep.
  const p = plan({ a: T(2) }, {}, { a: J(1, 'x') });
  const s = only(p, 'settle');
  ok(s.length === 1 && s[0].why === 'deleted on both', 'deleted on both → settle');
}
{ // Delete loses to edit (their edit): we deleted, they edited → fetch it back.
  const p = plan({ a: F(2, 'y') }, {}, { a: J(1, 'x') });
  const f = only(p, 'fetch');
  ok(f.length === 1 && /keeping the edit/.test(f[0].why), 'our delete loses to their edit');
}
{ // Delete loses to edit (our edit): they tombstoned, we edited → resurrect.
  const p = plan({ a: T(2) }, { a: D('z') }, { a: J(1, 'x') });
  const s = only(p, 'send');
  ok(s.length === 1 && s[0].resurrect === true && s[0].v === 3, 'their delete loses to our edit → resurrect');
}
{ // Divergent bytes: both changed, different content → exactly one conflict, named after the writer.
  const p = plan({ a: F(2, 'y', { by: 'tablet', mtime: 7777000 }) }, { a: D('z') }, { a: J(1, 'x') });
  const k = only(p, 'keepBoth');
  ok(k.length === 1 && /conflict from tablet/.test(k[0].keepAs), 'divergent → keepBoth named after writer');
}
{ // Same bytes landed independently: both "changed", identical csum → settle, no transfer, no conflict.
  const p = plan({ a: F(2, 'q') }, { a: D('q') }, { a: J(1, 'x') });
  const s = only(p, 'settle');
  ok(s.length === 1 && s[0].why === 'same content both sides', 'identical bytes settle');
}
{ // Excluded paths are invisible in every direction.
  const p = plan({ 'x/a': F(1, 'x') }, { 'x/b': D('y') }, {}, { excludes: ['x'] });
  ok(p.excluded === 2 && p.fetch.length === 0 && p.send.length === 0, 'excluded drops both sides');
}

/* ---- the rules that are new because per-file records are new --------------------------------- */

{ // OUR OWN PUBLISH COMING BACK: record one ahead of journal, same content on disk → settle, never
  // a self-download. (The record legitimately runs ahead for the length of one checkpoint.)
  const p = plan({ a: F(2, 'x', { by: 'me' }) }, { a: D('x') }, { a: J(1, 'x') });
  const s = only(p, 'settle');
  ok(s.length === 1 && s[0].why === 'same content both sides', 'own publish ahead → adopt, not fetch');
}
{ // JOURNAL AHEAD OF THE RECORD IS NEVER REMOTE NEWS — the silent-revert rule, kept.
  const p = plan({ a: F(1, 'x') }, { a: D('y', { mtime: 2000, size: 11 }) },
                 { a: Object.assign(J(2, 'y'), { local: { size: 11, mtime: 2000, csum: 'y' } }) });
  ok(p.unchanged === 1, 'journal ahead → not "changed elsewhere", nothing re-fetched');
  only(p, '__none__');
}
{ // A LOST RECORD IS RESTORED by whoever holds the file.
  const p = plan({}, { a: D('x') }, { a: J(3, 'x') });
  const s = only(p, 'send');
  ok(s.length === 1 && /restoring it/.test(s[0].why) && s[0].v === 4, 'lost record → re-published from this copy');
}
{ // A lost TOMBSTONE record restores nothing: no record + no file = a path nobody claims.
  const p = plan({}, {}, { a: Object.assign(J(3, 'x'), { deletedAt: 5000, local: undefined }) });
  only(p, '__none__');
  ok(p.unchanged === 1, 'lost tombstone record → no-op');
}
{ // An address-less record with a local copy is re-sent.
  const p = plan({ a: { v: 2, by: 'other', size: 10, mtime: 1000, csum: 'x' } },
                 { a: D('x') }, { a: J(2, 'x') });
  const s = only(p, 'send');
  ok(s.length === 1 && /names no storage/.test(s[0].why) && s[0].v === 3, 'address-less record → resend');
}

/* ---- THE DIRTY JOIN, the scenario that killed engine two ------------------------------------- */
{
  // A device with a hashed scan (thin journal forces one), 12 files on disk, 12 records: 10
  // identical, 2 divergent. Wanted: EXACTLY 2 conflicts, 10 settles, zero silent winners.
  const state = {}, disk = {};
  for(let i = 0; i < 10; i++){ state['f' + i] = F(1, 'same' + i); disk['f' + i] = D('same' + i); }
  state.d1 = F(1, 'theirs1', { by: 'desktop' }); disk.d1 = D('ours1');
  state.d2 = F(1, 'theirs2', { by: 'desktop' }); disk.d2 = D('ours2');
  const p = plan(state, disk, {});
  ok(p.settle.filter(s => s.why === 'same content both sides').length === 10, 'dirty join: identical bytes settle');
  ok(p.keepBoth.length === 2, 'dirty join: EXACTLY the divergent pair conflicts, got ' + p.keepBoth.length);
  ok(p.fetch.length === 2 || p.keepBoth.length === 2, 'dirty join: divergence resolved as conflict, not overwrite');
  ok(p.send.length === 0 && p.remove.length === 0 && p.tombstone.length === 0,
     'dirty join: nothing uploaded, trashed or tombstoned');
}

{ // A joining device's UNCHANGED copy of a deliberately deleted file obeys the deletion…
  const p1 = plan({ a: T(2, { csum: 'x' }) }, { a: D('x') }, {});
  const t = only(p1, 'remove');
  ok(t.length === 1 && /the deleted version/.test(t[0].why), 'join with the deleted bytes → deletion applies');
  // …and an EDITED copy still wins over the delete.
  const p2 = plan({ a: T(2, { csum: 'x' }) }, { a: D('edited') }, {});
  const s2 = only(p2, 'send');
  ok(s2.length === 1 && s2[0].resurrect === true, 'join with edited bytes → edit wins');
  // …and with no csum to compare, the safe direction is keeping the file.
  const p3 = plan({ a: T(2) }, { a: D('x') }, {});
  ok(only(p3, 'send').length === 1, 'no comparable csum → the file survives');
}

/* ---- guards ---------------------------------------------------------------------------------- */
/* ---- THE DELETION GUARDS ARE GONE, AND THAT IS THE POINT --------------------------------------
 *
 * There used to be a floor, a ratio and a cap in each direction, and a dialog whenever one fired.
 * Every one was added after a real loss and every one was locally correct. Together they were a
 * system nobody could predict: the bands BETWEEN them were silent (59 stale tombstones against a
 * 1,000-file folder passed the ratio AND a cap of 100 and ran with no verdict at all), they were
 * asymmetric (the one device still holding good copies was the one the resurrect floor refused),
 * and a dialog that fires often enough is a dialog people confirm — which is how "Mirror this
 * Device" took 122 files off every machine.
 *
 * They were all approximating one question: CAN THIS DELETION BE UNDONE? That is now answered
 * directly, per file, by the executor — the local copy goes only once the STORE IS CONFIRMED to
 * hold those bytes, and the bytes stay there in one account-wide trash on the server. A number
 * standing in for safety could not tell a deliberate bulk delete from a folder about to be lost.
 * The measurement does not have to.
 */
{
  // 25 deletions against 3 kept files — the shape that used to be refused outright.
  const state = {}, disk = {}, index = {};
  for(let i = 0; i < 25; i++){ const k = 't' + i;
    state[k] = T(2); disk[k] = D('c' + i); index[k] = J(1, 'c' + i); }
  for(let i = 0; i < 3; i++){ const k = 'k' + i;
    state[k] = F(1, 's' + i); disk[k] = D('s' + i); index[k] = J(1, 's' + i); }
  const p = plan(state, disk, index);
  ok(p.remove.length === 25, 'setup: 25 planned deletions');
  const v = S.check(p, { state });
  ok(v.every(x => x.kind !== 'massTrash'), 'a count-based deletion guard came back');
  ok(S.apply(p, v, []).remove.length === 25,
     'something is still suppressing deletions by the number of them');
}
{
  // 150 deletions on a 10,000-file folder: passes every ratio, and used to be caught by the floor.
  const state = {}, disk = {}, index = {};
  for(let i = 0; i < 10000; i++){ const k = 'u' + i;
    state[k] = F(1, 'c' + i); disk[k] = D('c' + i); index[k] = J(1, 'c' + i); }
  for(let i = 0; i < 150; i++){ const k = 'g' + i;
    state[k] = T(2); disk[k] = D('g' + i); index[k] = J(1, 'g' + i); }
  const p = plan(state, disk, index);
  ok(p.remove.length === 150, 'setup: 150 planned deletions');
  ok(S.check(p, { state }).every(x => x.kind !== 'massTrash'), 'the deletion floor came back');
}
{
  /* THE ONE RULE THAT SURVIVED, and it is not a count: a device that can see NONE of the files it
   * knows about has lost sight of the folder — a revoked grant, an unmounted volume, a folder
   * picked at the wrong path. That is a different statement from "somebody emptied it", there is no
   * answer a person could give that makes it right, and the store cannot help: the question is not
   * whether the bytes survive but whether this device is entitled to an opinion at all. */
  const state2 = {}, disk2 = {}, index2 = {};
  for(let i = 0; i < 150; i++){ const k = 'g' + i; state2[k] = F(1, 'g' + i); index2[k] = J(1, 'g' + i); }
  const p2 = plan(state2, disk2, index2);
  ok(p2.tombstone.length === 150, 'setup: 150 planned tombstones');
  const v2 = S.check(p2, { state: state2 });
  const lost = v2.find(x => x.kind === 'massTombstone');
  ok(!!lost && lost.rule === 'emptyDevice' && lost.fatal === true,
     'a device that can see nothing is still allowed to delete the folder everywhere');
  ok(S.apply(p2, v2, ['massTombstone']).tombstone.length === 0,
     'the lost-folder rule is fatal and may not be confirmed away');

  // …and a device that can still see SOME of its folder is simply believed.
  const state3 = {}, disk3 = {}, index3 = {};
  for(let i = 0; i < 40; i++){ const k = 'h' + i;
    state3[k] = F(1, 'h' + i); disk3[k] = D('h' + i); index3[k] = J(1, 'h' + i); }
  for(let i = 0; i < 150; i++){ const k = 'g' + i; state3[k] = F(1, 'g' + i); index3[k] = J(1, 'g' + i); }
  const p3 = plan(state3, disk3, index3);
  ok(p3.tombstone.length === 150, 'setup: 150 tombstones beside 40 live files');
  ok(S.check(p3, { state: state3 }).every(x => x.kind !== 'massTombstone'),
     'a device that can see its folder was still refused');
}
{
  // A restored backup republishes what others deleted — absolute floor, no ratio.
  const state = {}, disk = {}, index = {};
  for(let i = 0; i < 25; i++){ const k = 'r' + i;
    state[k] = T(2); disk[k] = D('new' + i, { mtime: 3000 }); index[k] = J(1, 'old' + i); }
  const p = plan(state, disk, index);
  const v = S.check(p, { state });
  ok(v.some(x => x.kind === 'massResurrect'), 'massResurrect fires at 25');
  const applied = S.apply(p, v, []);
  ok(applied.send.every(s => !s.resurrect), 'refused resurrections drop only the resurrections');
}
{
  // File-vs-folder collision is fatal and cannot be allowed through.
  const state = { a: F(2, 'x'), 'a/b': F(1, 'y') };
  const p = plan(state, {}, {});
  const v = S.check(p, { state });
  const b = v.find(x => x.kind === 'blocked');
  ok(!!b && b.fatal === true, 'file/folder collision is fatal');
  ok(S.apply(p, v, ['blocked']).fetch.every(f => f.path !== 'a'), 'fatal verdicts ignore allows');
}

/* ---- one file, two names: the folding-filesystem trap ---------------------------------------- */
{
  // Photo.jpg and photo.jpg are two legitimate records (a Linux device holds both) and ONE file on
  // Windows/macOS. Writing both makes the records climb versions against each other for ever — so
  // on a folding device only the winner may be written, and the twin is refused BY NAME.
  const state = { 'Photo.jpg': F(3, 'a', { by: 'linux' }), 'photo.jpg': F(1, 'b', { by: 'linux' }) };
  const p1 = plan(state, {}, {});
  const v = S.check(p1, { state, caseFolds: true });
  const b = v.filter(x => x.kind === 'blocked');
  ok(b.length === 1 && b[0].path === 'photo.jpg' && b[0].fatal === true,
     'the losing twin was not refused: ' + JSON.stringify(b));
  const applied = S.apply(p1, v, []);
  ok(applied.fetch.length === 1 && applied.fetch[0].path === 'Photo.jpg',
     'a folding device fetched both twins');
  // …and a NON-folding device (Linux) fetches both, because there they are two real files.
  const v2 = S.check(p1, { state, caseFolds: false });
  ok(v2.every(x => x.kind !== 'blocked'), 'a case-sensitive device refused legitimate twins');
  // macOS's other trap: NFC vs NFD spellings of the same name fold together too.
  const nfc = 'café.txt', nfd = 'café.txt';
  const st2 = {}; st2[nfc] = F(2, 'x'); st2[nfd] = F(1, 'y');
  const p2 = plan(st2, {}, {});
  const v3 = S.check(p2, { state: st2, caseFolds: true });
  ok(v3.some(x => x.kind === 'blocked' && x.path === nfd),
     'NFD/NFC twins were not detected: ' + JSON.stringify(v3));
  // A blocked twin also never resolves as a conflict write.
  const st3 = { 'A.txt': F(2, 'p', { by: 'x' }), 'a.txt': F(2, 'q', { by: 'y' }) };
  const p3 = plan(st3, { 'a.txt': D('local') }, {});
  const v4 = S.check(p3, { state: st3, caseFolds: true });
  const ap = S.apply(p3, v4, []);
  ok(ap.keepBoth.every(k => k.path !== 'a.txt') || ap.keepBoth.every(k => k.path !== 'A.txt'),
     'both twins were written through the conflict path');
}

/* ---- determinism ------------------------------------------------------------------------------ */
{
  const mk = (order) => {
    const state = {}, disk = {}, index = {};
    for(const i of order){
      state['p' + i] = (i % 3 === 0) ? T(2) : F(2, 'c' + i, { by: 'dev' + (i % 4) });
      if(i % 2 === 0) disk['p' + i] = D((i % 5 === 0) ? 'local' + i : 'c' + i);
      if(i % 4 !== 3) index['p' + i] = J(1, 'c' + i);
    }
    return plan(state, disk, index);
  };
  const a = mk([...Array(50).keys()]);
  const b = mk([...Array(50).keys()].reverse());
  eq(a, b, 'the plan is independent of input order');
}

/* ---- HOW MANY TIMES A REPAIR HAS BEEN TRIED -----------------------------------------------------
 *
 * The refusal that stops a bad copy being fetched twice is keyed on its STORAGE ADDRESS, so that a
 * genuine repair — new bytes, new ciphertext, new address — lifts it without anybody asking. That is
 * exactly right, and it is also the hole: a device whose checksum of its own file is wrong agrees
 * with itself, re-sends the same content under a new address, and clears every other device's
 * memory. Sixteen rounds in ninety minutes on one file, measured, with nothing able to end it.
 * These are the rules that count the rounds THROUGH the address changing. */
{
  const bad = (id, why) => ({ [P]: { id, why: why || 'checksum', v: 1 } });
  const P = 'DCIM/img0.jpg';

  let m = S.mergeBadFetch({}, bad('a1'));
  eq(m[P].rounds, 1, 'repair: the first failure is not round one');

  m = S.mergeBadFetch(m, bad('a2'));
  eq(m[P].rounds, 2, 'repair: a DIFFERENT copy failing the same way did not count as another round');

  m = S.mergeBadFetch(m, bad('a2'));
  eq(m[P].rounds, 2, 'repair: the same copy failing twice was counted as two separate copies');

  eq(S.repairExhausted(m[P], false), false, 'repair: gave up after two copies');
  m = S.mergeBadFetch(m, bad('a3'));
  eq(S.repairExhausted(m[P], false), true, 'repair: three failed copies did not exhaust it');

  // A PERSON PRESSING THE BUTTON is answering the very question the count exists to ask.
  eq(S.repairExhausted(m[P], true), false, 'repair: a manual sweep was refused by the automatic guard');

  // A DIFFERENT KIND OF FAILURE SAYS NOTHING ABOUT THE LAST ONE. Bytes the store has lost are not
  // evidence about anybody's checksum, and counting them together would abandon a file over a media
  // server having one bad minute.
  let g = S.mergeBadFetch({}, bad('a1'));
  g = S.mergeBadFetch(g, bad('a2'));
  g = S.mergeBadFetch(g, bad('a3', 'gone'));
  eq(g[P].rounds, 1, 'repair: a 404 inherited a checksum failure\u2019s round count');
  eq(S.repairExhausted(g[P], false), false, 'repair: a 404 was treated as an exhausted repair');

  // AN UPGRADE IS NOT AN EXHAUSTED REPAIR. Older builds stored a bare address string here; read as
  // a round count that would abandon files on the first failure after an update.
  const old = S.mergeBadFetch({ [P]: 'an-old-bare-address' }, bad('a2'));
  eq(old[P].rounds, 2, 'repair: an older build\u2019s memory did not count as one round');
  eq(S.repairExhausted('an-old-bare-address', false), false,
     'repair: an older build\u2019s bare string read as an exhausted repair');
}

console.log(failures ? ('state_sim: ' + failures + ' of ' + checks + ' checks FAILED')
                     : ('state_sim: all ' + checks + ' checks passed'));
process.exit(failures ? 1 : 0);
