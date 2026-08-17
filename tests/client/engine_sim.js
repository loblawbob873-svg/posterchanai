/* THE RECONCILER, EXHAUSTIVELY — including the case the old design could not express at all:
 * two or more devices updating the same folder at the same time.
 *
 * Pure, so every case is a table entry rather than a machine. What it proves:
 *
 *   1. the merge is DETERMINISTIC — every device computes the same folder from the same views,
 *      whatever order it reads them in, or they take turns undoing each other for ever;
 *   2. concurrent edits are detected as concurrent and both copies survive;
 *   3. no single view can empty the folder, missing or wrong;
 *   4. the state table has no fall-through: every combination of (disk, folder, journal) produces
 *      exactly one action, and the same one every time;
 *   5. the guards fire on shape, and refusing one kind of action never suppresses the others.
 *
 * Usage: node engine_sim.js
 */
'use strict';
const path = require('path');
require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'foldersync.js'));
const E = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncengine.js'));

const fail = [];
const T = (name, fn) => { try{ fn((c, w) => { if(!c) fail.push(name + ': ' + w); }); }
                          catch(e){ fail.push(name + ' threw: ' + ((e && e.stack) || e)); } };

const file = (v, by, csum, extra) => Object.assign(
  { v, by, csum, sha: 'blob-' + csum, size: 100, mtime: 1000 }, extra || {});
const tomb = (v, by, at) => ({ v, by, deletedAt: at || 5000 });
const onDisk = (csum, extra) => Object.assign({ csum, size: 100, mtime: 1000 }, extra || {});
const applied = (e, local) => Object.assign({}, e, { local: local || { size: 100, mtime: 1000, csum: e.csum } });

const plan = (o) => {
  const m = E.merge(o.views || {});
  return E.reconcile({ disk: o.disk || {}, global: m.global, rivals: m.rivals, by: m.by,
                       index: o.index || {}, device: o.device || 'me', now: o.now || 9000,
                       excludes: o.excludes || [] });
};

/* ---- 1. the merge ---------------------------------------------------------------------------- */

T('the newest version of a path wins whoever published it', (ck) => {
  const m = E.merge({ laptop: { 'a.txt': file(3, 'laptop', 'AAA') },
                      phone:  { 'a.txt': file(7, 'phone',  'BBB') } });
  ck(m.global['a.txt'].csum === 'BBB', 'the older version won');
  ck(!m.rivals['a.txt'], 'an out-of-date copy was mistaken for a concurrent edit');
});

T('the merge does not depend on the order the views are read in', (ck) => {
  const A = { 'a.txt': file(4, 'aaa', 'X'), 'b.txt': tomb(2, 'aaa') };
  const B = { 'a.txt': file(4, 'bbb', 'Y'), 'b.txt': file(1, 'bbb', 'Z') };
  const C = { 'c.txt': file(9, 'ccc', 'W') };
  const one = E.merge({ aaa: A, bbb: B, ccc: C });
  const two = E.merge({ ccc: C, bbb: B, aaa: A });
  ck(JSON.stringify(one.global) === JSON.stringify(two.global), 'the merge changed with read order');
  ck(JSON.stringify(one.rivals) === JSON.stringify(two.rivals), 'the rivals changed with read order');
});

T('a delete published later than an edit wins the merge', (ck) => {
  const m = E.merge({ laptop: { 'a.txt': file(3, 'laptop', 'AAA') },
                      phone:  { 'a.txt': tomb(4, 'phone') } });
  ck(!!m.global['a.txt'].deletedAt, 'the delete did not win');
});

T('two devices publishing the same version with different bytes is a concurrent edit', (ck) => {
  const m = E.merge({ laptop: { 'a.txt': file(5, 'laptop', 'AAA') },
                      phone:  { 'a.txt': file(5, 'phone',  'BBB') } });
  ck(!!m.rivals['a.txt'], 'a concurrent edit was not detected');
  ck(m.rivals['a.txt'].by !== m.by['a.txt'], 'the rival is the same claim as the winner');
});

T('the same version with the same bytes is not a conflict', (ck) => {
  const m = E.merge({ laptop: { 'a.txt': file(5, 'laptop', 'SAME') },
                      phone:  { 'a.txt': file(5, 'phone',  'SAME') } });
  ck(!m.rivals['a.txt'], 'identical content was reported as a conflict');
});

/* ---- 2. concurrent edits reach the same answer on every device ------------------------------- */

T('every device resolves a concurrent edit identically', (ck) => {
  const views = { laptop: { 'a.txt': file(5, 'laptop', 'AAA') },
                  phone:  { 'a.txt': file(5, 'phone',  'BBB') } };
  const seen = new Set();
  for(const me of ['laptop', 'phone', 'tablet']){
    const p = plan({ views, device: me, disk: {}, index: {} });
    ck(p.keepBoth.length === 1, me + ' did not keep both copies');
    seen.add(p.keepBoth[0].path + ' -> ' + p.keepBoth[0].keepAs);
  }
  ck(seen.size === 1, 'the devices disagreed about the outcome: ' + [...seen].join(' | '));
});

/* ---- 3. no single view can empty the folder --------------------------------------------------- */

T('one device publishing an empty view deletes nothing', (ck) => {
  const good = {}, mine = {};
  for(let i = 0; i < 100; i++){
    good['p' + i] = file(1, 'laptop', 'c' + i);
    mine['p' + i] = onDisk('c' + i);
  }
  const index = {}; for(const p in good) index[p] = applied(good[p]);
  const p = plan({ views: { laptop: good, broken: {} }, disk: mine, index, device: 'me' });
  ck(p.trash.length === 0, 'an empty view from one device proposed ' + p.trash.length + ' deletions');
  ck(p.unchanged === 100, 'the folder was not read as settled (' + p.unchanged + ')');
});

T('a view that could not be read stops every deletion, and nothing else', (ck) => {
  const index = { 'a.txt': applied(file(1, 'laptop', 'AAA')) };
  const p = plan({ views: { laptop: { 'a.txt': tomb(2, 'laptop'), 'c.txt': file(1, 'laptop', 'CCC') } },
                   disk: { 'a.txt': onDisk('AAA'), 'b.txt': onDisk('BBB') }, index });
  ck(p.trash.length === 1, 'the plan did not propose the one real deletion');
  const v = E.check(p, { missingViews: 1 });
  ck(v.some(x => x.kind === 'partialViews' && x.fatal), 'a missing view did not stop the deletions');
  const out = E.apply(p, v, ['partialViews']);            // …and it cannot be waved through
  ck(out.trash.length === 0, 'a fatal refusal was overridden');
  ck(out.fetch.length === 1, 'refusing a deletion also stopped the download');
});

/* ---- 4. the state table: one action per combination, always the same one ---------------------- */

const CASES = [
  // [name, disk, folder, journal, expected bucket]
  ['new here',                    onDisk('A'),   null,               null,               'send'],
  ['new elsewhere',               null,          file(1,'x','A'),    null,               'fetch'],
  ['settled',                     onDisk('A'),   file(1,'x','A'),    applied(file(1,'x','A')), 'unchanged'],
  ['changed here',                onDisk('B'),   file(1,'x','A'),    applied(file(1,'x','A')), 'send'],
  ['changed elsewhere',           onDisk('A'),   file(2,'x','B'),    applied(file(1,'x','A')), 'fetch'],
  ['deleted here',                null,          file(1,'x','A'),    applied(file(1,'x','A')), 'tombstone'],
  ['deleted elsewhere',           onDisk('A'),   tomb(2,'x'),        applied(file(1,'x','A')), 'trash'],
  ['deleted on both',             null,          tomb(2,'x'),        applied(file(1,'x','A')), 'settle'],
  ['already gone here',           null,          tomb(2,'x'),        null,               'settle'],
  ['same content both sides',     onDisk('A'),   file(2,'x','A'),    null,               'settle'],
  ['edited here, deleted there',  onDisk('B'),   tomb(2,'x'),        applied(file(1,'x','A')), 'send'],
  ['deleted here, edited there',  null,          file(2,'x','B'),    applied(file(1,'x','A')), 'fetch'],
  ['edited on both',              onDisk('B'),   file(2,'x','C'),    applied(file(1,'x','A')), 'keepBoth'],
];

T('every state combination lands in exactly one bucket', (ck) => {
  for(const [name, L, R, idx, want] of CASES){
    const p = plan({ views: R ? { x: { 'f.dat': R } } : {},
                     disk: L ? { 'f.dat': L } : {},
                     index: idx ? { 'f.dat': idx } : {} });
    const buckets = ['fetch','send','trash','tombstone','keepBoth','settle']
                      .filter(k => p[k].length);
    const got = buckets.length ? buckets[0] : (p.unchanged ? 'unchanged' : 'NOTHING');
    ck(buckets.length <= 1, name + ': landed in ' + buckets.length + ' buckets (' + buckets.join(',') + ')');
    ck(got === want, name + ': expected ' + want + ', got ' + got);
  }
});

T('the same inputs always produce the same plan', (ck) => {
  for(const [name, L, R, idx] of CASES){
    const mk = () => plan({ views: R ? { x: { 'f.dat': R } } : {}, disk: L ? { 'f.dat': L } : {},
                            index: idx ? { 'f.dat': idx } : {} });
    ck(JSON.stringify(mk()) === JSON.stringify(mk()), name + ' is not deterministic');
  }
});

T('a published version is always ahead of everything either side has seen', (ck) => {
  const p = plan({ views: { x: { 'f.dat': file(9, 'x', 'A') } },
                   disk: { 'f.dat': onDisk('B') },
                   index: { 'f.dat': applied(file(9, 'x', 'A')) } });
  ck(p.send.length === 1, 'a local edit was not published (' + JSON.stringify(p) + ')');
  ck(p.send[0].v === 10, 'published v' + p.send[0].v + ', expected 10');
});

/* A CHECKSUM IN THE JOURNAL IS WHAT MAKES A RESTORED BACKUP HARMLESS. The mtime moved and the bytes
 * did not, so there is no edit here to weigh against anybody's deletion — the case that used to
 * republish thousands of files does not even reach the rule that would have done it. */
T('a backup restore that changes only timestamps is not an edit', (ck) => {
  const p = plan({ views: { x: { 'f.dat': tomb(2, 'x') } },
                   disk: { 'f.dat': onDisk('A', { mtime: 999999 }) },
                   index: { 'f.dat': applied(file(1, 'x', 'A')) } });
  ck(p.send.length === 0, 'a touched-but-identical file was republished');
  ck(p.trash.length === 1, 'the deletion was not applied');
});

/* ---- 5. the guards ---------------------------------------------------------------------------- */

T('a mass trash is refused, and only the trash is refused', (ck) => {
  const views = { x: {} }, disk = {}, index = {};
  for(let i = 0; i < 50; i++){
    views.x['g' + i] = tomb(2, 'x');
    disk['g' + i] = onDisk('c' + i);
    index['g' + i] = applied(file(1, 'x', 'c' + i));
  }
  disk['fresh.txt'] = onDisk('NEW');                    // one ordinary upload alongside
  const p = plan({ views, disk, index });
  const v = E.check(p, {});
  ck(v.some(x => x.kind === 'massTrash'), 'a 50-file trash was not questioned');
  const out = E.apply(p, v, []);
  ck(out.trash.length === 0, 'the refusal did not suppress the deletions');
  ck(out.send.length === 1, 'the refusal also suppressed the upload');
});

T('a person may allow a mass trash', (ck) => {
  const views = { x: {} }, disk = {}, index = {};
  for(let i = 0; i < 50; i++){
    views.x['g' + i] = tomb(2, 'x'); disk['g' + i] = onDisk('c' + i);
    index['g' + i] = applied(file(1, 'x', 'c' + i));
  }
  const p = plan({ views, disk, index });
  const out = E.apply(p, E.check(p, {}), ['massTrash']);
  ck(out.trash.length === 50, 'a confirmed mass trash was still refused');
});

T('a restored backup does not republish everybody else\'s deletions', (ck) => {
  const views = { x: {} }, disk = {}, index = {};
  for(let i = 0; i < 40; i++){
    views.x['g' + i] = tomb(2, 'x');
    disk['g' + i] = onDisk('c' + i, { mtime: 999999 });   // an rsync without -t
    // …on a folder this device has never hashed, which is the ordinary case: an incremental sweep
    // records size and mtime, so a moved timestamp is all this device knows about the file.
    index['g' + i] = applied(file(1, 'x', 'c' + i), { size: 100, mtime: 1000 });
  }
  const p = plan({ views, disk, index });
  ck(p.send.filter(s => s.resurrect).length === 40, 'the resurrections were not identified as such');
  const out = E.apply(p, E.check(p, {}), []);
  ck(out.send.length === 0, 'a restored backup republished ' + out.send.length + ' deleted files');
});

T('a file that is a folder somewhere else is never written', (ck) => {
  const views = { x: { 'notes': file(1, 'x', 'A'), 'notes/todo.md': file(1, 'x', 'B') } };
  const p = plan({ views, disk: {}, index: {} });
  const m = E.merge(views);
  const v = E.check(p, { global: m.global });
  ck(v.some(x => x.kind === 'blocked' && x.fatal), 'a file/folder collision was not caught');
  const out = E.apply(p, v, ['blocked']);
  ck(out.fetch.length === 1, 'the collision was written anyway, or the sibling was dropped too');
});

T('an exclusion hides a path from all three sides and deletes nothing', (ck) => {
  const views = { x: { 'Old/a.jpg': file(1, 'x', 'A'), 'New/b.jpg': file(1, 'x', 'B') } };
  const p = plan({ views, disk: {}, index: { 'Old/a.jpg': applied(file(1, 'x', 'A')) },
                   excludes: ['Old'] });
  ck(p.excluded === 1, 'the exclusion did not drop the path');
  ck(p.trash.length === 0 && p.tombstone.length === 0, 'an exclusion proposed a deletion');
  ck(p.fetch.length === 1, 'the exclusion swallowed an unrelated file');
});

if(fail.length){ console.log('FAIL\n  ' + fail.join('\n  ')); process.exit(1); }
console.log('OK  reconciler: merge, concurrency, the state table and every guard');
