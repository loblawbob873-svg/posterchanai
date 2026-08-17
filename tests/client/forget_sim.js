/* FORGETTING A FOLDER MUST ACTUALLY EMPTY THE RECORD.
 *
 * Reported as "how can 8K always be removed": pressing forget said "8,132 entries cleared" every
 * single time, and the folder never went away. It never cleared anything.
 *
 * `save()` re-reads and merges whenever it is given a `touched` list, and the merge writes a path
 * only `if(paths[p] !== undefined)` — a missing key means "leave it alone", which is why every
 * deletion in this feature is a TOMBSTONE rather than a removed key. `forget` passed an EMPTY
 * manifest plus all 8,132 paths, so every lookup was undefined, every assignment was skipped, and the
 * document was written back unchanged. The POST succeeded, so it reported success.
 *
 * The stub below implements that merge EXACTLY as sync.js does, because a stub that simply stored
 * whatever it was handed would have passed against the broken code — which is how this shipped.
 *
 * Usage: node forget_sim.js
 */
'use strict';
const fail = [];
const check = (c, w) => { if(!c) fail.push(w); };

function makeStore(initial){
  let doc = Object.assign({}, initial);
  return {
    saves: 0,
    async manifest(){ return JSON.parse(JSON.stringify(doc)); },
    /* The real merge, copied in shape from sync.js's store.save: with `touched`, a path is written
     * only when it is PRESENT in the manifest handed in. Without `touched`, the manifest replaces
     * the document wholesale. */
    async save(key, s){
      this.saves++;
      this.lastSave = s;
      let paths = s.manifest || {};
      if(Array.isArray(s.touched)){
        const merged = Object.assign({}, doc);
        for(const p of s.touched) if(paths[p] !== undefined) merged[p] = paths[p];
        paths = merged;
      }
      doc = JSON.parse(JSON.stringify(paths));
    },
  };
}

/* The shipped forget(), lifted from sync.js so this drives the real logic rather than a paraphrase. */
function loadForget(store){
  const fs = require('fs'), path = require('path');
  const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'sync.js'), 'utf8');
  const at = src.indexOf('async forget(key){');
  if(at < 0) throw new Error('forget() is gone from sync.js');
  let i = src.indexOf('{', at), depth = 0, end = i;
  while(i < src.length){
    if(src[i] === '{') depth++;
    else if(src[i] === '}'){ depth--; if(depth === 0){ end = i; break; } }
    i++;
  }
  const body = src.slice(at, end + 1);
  // eslint-disable-next-line no-new-func
  return new Function('store', 'return ({ ' + body + ' }).forget;')(store);
}

(async () => {
  const N = 8132;
  const initial = {};
  for(let i = 0; i < N; i++) initial['Pictures/p' + i + '.jpg'] = { deletedAt: 4000 };
  initial['Pictures/live.jpg'] = { size: 10, mtime: 1 };

  const store = makeStore(initial);
  const forget = loadForget(store);

  const out = await forget('Pictures');
  const left = Object.keys(await store.manifest('Pictures')).length;

  check(left === 0, 'the record still holds ' + left + ' entries — nothing was cleared');
  check(out && out.removed === N + 1, 'reported ' + (out && out.removed) + ' cleared, expected ' + (N + 1));
  check(out && out.live === 1, 'the live count was ' + (out && out.live) + ', expected 1');
  check(out && out.tombstones === N, 'the tombstone count was wrong');

  // …and a second press finds nothing, which is the thing that never happened before.
  const again = await forget('Pictures');
  check(again && again.removed === 0,
        'a second forget reported ' + (again && again.removed) + ' cleared — it is still lying');

  /* The wipe has to be RECOGNISABLE to the server, which cannot read a sealed manifest. sync.js's
   * save() publishes a plaintext `entries` count beside `n`; without it a forgotten folder stays in
   * the account list for ever, which is what "Pictures never removed in Blossom" was. */
  check(store.lastSave && !Array.isArray(store.lastSave.touched),
        'the wipe still passes `touched`, which makes save() merge and change nothing');

  console.log(JSON.stringify({ entriesBefore: N + 1, entriesAfter: left,
                               passedTouched: !!(store.lastSave && store.lastSave.touched),
                               reported: out && out.removed, live: out && out.live,
                               tombstones: out && out.tombstones,
                               secondPress: again && again.removed, failures: fail }, null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
