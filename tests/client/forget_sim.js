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
function makeWorld(state){
  const w = {
    era: 0,
    recs: JSON.parse(JSON.stringify(state)),      // path -> entry, all at era 0
    posts: [],
    cacheCleared: 0, journalCleared: 0,
    /* What forget() talks to: the state store's surface, with the endpoint's era rule — one bump,
     * and every record is of a dead world. */
    stateS: {
      async load(key){
        const out = {};
        if(w.era === 0) for(const p in w.recs) out[p] = JSON.parse(JSON.stringify(w.recs[p]));
        return { state: out, flagged: {}, era: w.era };
      },
      async clear(key){ w.cacheCleared++; },
    },
    async _statePost(body){
      w.posts.push(body);
      if(body.forgetAll){ w.era++; return { ok: true, era: w.era }; }
      return { ok: true, era: w.era };
    },
    async _saveBase(key, v){ if(!Object.keys(v || {}).length) w.journalCleared++; },
  };
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
  return new Function('stateS', '_statePost', '_saveBase', 'S_ENGINE',
                      'return ({ ' + body + ' }).forget;')(
    world.stateS, world._statePost, world._saveBase, engine);
}

(async () => {
  const path = require('path');
  require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'foldersync.js'));
  const engine = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncstate.js'));

  const N = 8132;
  const state = {};
  for(let i = 0; i < N; i++) state['Pictures/p' + i + '.jpg'] = { v: 2, by: 'laptop', deletedAt: 4000 };
  state['Pictures/live.jpg'] = { v: 1, by: 'phone', size: 10, mtime: 1, sha: 's' };

  const world = makeWorld(state);
  const forget = loadForget(world, engine);

  const out = await forget('Pictures');
  const after = await world.stateS.load('Pictures');
  const left = Object.keys(after.state).length;

  check(left === 0, 'the record still holds ' + left + ' entries — the era did not kill the world');
  check(world.posts.some(p => p && p.forgetAll), 'it never asked the server to retire the pair');
  check(out && out.removed === N + 1, 'reported ' + (out && out.removed) + ' cleared, expected ' + (N + 1));
  check(out && out.live === 1, 'the live count was ' + (out && out.live) + ', expected 1');
  check(out && out.tombstones === N, 'the tombstone count was wrong');
  check(world.cacheCleared >= 1, 'the local state cache survived the forget');
  check(world.journalCleared >= 1, 'the journal survived the forget — the next sweep resurrects');
  check(out && out.verified === true, 'the forget did not verify the server answer');

  // …and a second press finds nothing, which is the thing that never happened before.
  const again = await forget('Pictures');
  check(again && again.removed === 0,
        'a second forget reported ' + (again && again.removed) + ' cleared — it is still lying');

  console.log(JSON.stringify({
    failures: fail,
    entriesBefore: N + 1,
    entriesAfter: left,
    secondPress: again ? again.removed : -1,
    live: out ? out.live : -1,
    tombstones: out ? out.tombstones : -1,
    cacheCleared: world.cacheCleared,
    journalCleared: world.journalCleared,
  }));
  process.exit(fail.length ? 1 : 0);
})();
