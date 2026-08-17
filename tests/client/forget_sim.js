/* FORGETTING A FOLDER MUST ACTUALLY EMPTY THE RECORD — on every device, not just this one.
 *
 * Reported as "how can 8K always be removed": pressing forget said "8,132 entries cleared" every
 * single time, and the folder never went away. It never cleared anything.
 *
 * The mechanism has changed with the storage. There is no shared document any more — each device
 * publishes its own, and only that device ever writes it — so retiring a pair for the ACCOUNT is the
 * one deliberate exception to that rule: the server, which holds the storage key, empties them all.
 * Leave one behind and the folder returns the moment that device publishes again, and the name stays
 * unusable.
 *
 * What has NOT changed is why this test exists: the report is checked against a re-read, because
 * this once claimed success on a write that did nothing, and repeated the claim every time it was
 * pressed.
 *
 * The shipped forget() is lifted out of sync.js and RUN, so this drives the real logic rather than a
 * paraphrase of it.
 *
 * Usage: node forget_sim.js
 */
'use strict';
const fail = [];
const check = (c, w) => { if(!c) fail.push(w); };

/* The world forget() talks to: the per-device documents, and a server that can clear them all.
 *
 * `forgetAll` is modelled the way the endpoint implements it — every document for that pair replaced
 * with an empty one — so a forget that clears only its OWN document fails here, which is exactly the
 * bug this shape can have. */
function makeWorld(views){
  const docs = JSON.parse(JSON.stringify(views));
  const w = {
    docs,
    posts: [],
    async views(){ return { views: JSON.parse(JSON.stringify(docs)), missing: 0 }; },
    async publish(key, mine){ docs[w.me] = JSON.parse(JSON.stringify(mine)); },
    store: {
      async _post(body){
        w.posts.push(body);
        if(body.forgetAll){ for(const dev in docs) docs[dev] = {}; return { ok: true }; }
        return { ok: true };
      },
    },
  };
  w.me = 'laptop-a1b2';
  return w;
}

/** The shipped forget(), lifted from sync.js. */
function loadForget(world, engine){
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
  return new Function('docs', 'store', 'S_ENGINE', 'return ({ ' + body + ' }).forget;')(
    world, world.store, engine);
}

(async () => {
  const path = require('path');
  require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'foldersync.js'));
  const engine = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncengine.js'));

  const N = 8132;
  const laptop = {}, phone = {};
  for(let i = 0; i < N; i++) laptop['Pictures/p' + i + '.jpg'] = { v: 2, by: 'laptop', deletedAt: 4000 };
  laptop['Pictures/live.jpg'] = { v: 1, by: 'laptop', size: 10, mtime: 1 };
  // A second device holding the same folder. Clearing only our own document leaves this one, and the
  // folder comes straight back — the exact shape of "Pictures never removed in Blossom".
  phone['Pictures/live.jpg'] = { v: 1, by: 'phone', size: 10, mtime: 1 };

  const world = makeWorld({ 'laptop-a1b2': laptop, 'phone-c3d4': phone });
  const forget = loadForget(world, engine);

  const out = await forget('Pictures');
  const after = engine.merge((await world.views('Pictures')).views).global;
  const left = Object.keys(after).length;

  check(left === 0, 'the record still holds ' + left + ' entries — nothing was cleared');
  check(Object.keys(world.docs['phone-c3d4']).length === 0,
        'the other device’s document was left behind, so the folder comes back');
  check(out && out.removed === N + 1, 'reported ' + (out && out.removed) + ' cleared, expected ' + (N + 1));
  check(out && out.live === 1, 'the live count was ' + (out && out.live) + ', expected 1');
  check(out && out.tombstones === N, 'the tombstone count was wrong');
  check(out && out.devices === 2, 'it reported ' + (out && out.devices) + ' devices, expected 2');
  check(world.posts.some(p => p && p.forgetAll), 'it never asked the server to retire the pair');

  // …and a second press finds nothing, which is the thing that never happened before.
  const again = await forget('Pictures');
  check(again && again.removed === 0,
        'a second forget reported ' + (again && again.removed) + ' cleared — it is still lying');

  console.log(JSON.stringify({ entriesBefore: N + 1, entriesAfter: left,
                               devices: out && out.devices,
                               reported: out && out.removed, live: out && out.live,
                               tombstones: out && out.tombstones,
                               secondPress: again && again.removed, failures: fail }, null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
