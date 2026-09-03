/* THE FOLLOWS LIST RESETTING TO 2, AND WHY IT KEPT HAPPENING.
 *
 *   "the people I follow keeps getting reset to only 2 despite following them back over and over"
 *
 * Two halves of one ratchet. `fetchFollows` adopted whatever kind-3 came back, wholesale, and then
 * `_persistFollows` wrote that length into `followsCount` — which is the number `publish()`'s
 * shrink guard measures against. So the FIRST short read both shrank the list and disarmed the
 * guard that exists to stop the second. Following people back rebuilt the list; the next short read
 * flattened it again, for ever.
 *
 * Drives the SHIPPED fetchFollows/_persistFollows, extracted from app.js.
 */
import fs from 'node:fs';

const app = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
/* Anchored on _persistFollows, which exists in EVERY version of this file — not on the guard's own
 * variable. Anchoring on the fix means the pre-fix run dies at extraction with "fetchFollows moved",
 * which is a test that fails for the wrong reason and would hide a real behavioural regression
 * behind a plumbing error. */
const start = app.indexOf('  let _followShrinkWarned=false;');
const end = app.indexOf('  async function fetchMutes(){', start);
if (start < 0 || end < 0) throw new Error('fetchFollows moved');
const shipped = app.slice(start, end);

const ME = { pubkey: 'me'.padEnd(64, '0') };
const store = {};
globalThis.ME = ME;
globalThis.NO_IMAGES = true;
globalThis.VIEW = 'other';
globalThis.needProfile = () => {};
globalThis.renderView = () => {};
globalThis.toasts = [];
globalThis.toast = (m) => globalThis.toasts.push(m);
globalThis.ClientSettings = {
  get: (k, d) => (k in store ? store[k] : d),
  set: (k, v) => { store[k] = v; },
};
globalThis.storedEvents = [];
globalThis.Store = { all: () => globalThis.storedEvents };

const person = (n) => String(n).padStart(64, '0');
const kind3 = (n, id) => ({ id, created_at: 100 + n, tags: Array.from({length:n}, (_, i) => ['p', person(i)]) });

globalThis.relayAnswer = null;
globalThis.Relay = { query: async () => (globalThis.relayAnswer ? [globalThis.relayAnswer] : []) };

const run = new Function('kind3', 'person', `return (async()=>{
  let FOLLOWS = new Set();
  ${shipped}
  const out = {};

  // A healthy list: 40 follows, adopted normally and cached.
  globalThis.relayAnswer = kind3(40, 'healthy');
  await fetchFollows();
  out.adoptedHealthy = [...FOLLOWS].filter(p => p !== ME.pubkey).length;
  out.cachedAfterHealthy = ClientSettings.get('followsCount', 0);

  // Now a relay answers with two. This is the read that used to flatten everything.
  globalThis.relayAnswer = kind3(2, 'short');
  await fetchFollows();
  out.afterShortRead = [...FOLLOWS].filter(p => p !== ME.pubkey).length;
  out.cachedAfterShortRead = ClientSettings.get('followsCount', 0);
  out.said = globalThis.toasts.length;

  // A genuine, ordinary shrink (an unfollow or two) must STILL be adopted — the guard is about
  // wipes, not about refusing every list that got shorter.
  globalThis.relayAnswer = kind3(38, 'ordinary-unfollow');
  await fetchFollows();
  out.afterOrdinaryShrink = [...FOLLOWS].filter(p => p !== ME.pubkey).length;
  await fetchFollows();
  out.afterOrdinaryReload = [...FOLLOWS].filter(p => p !== ME.pubkey).length;

  // Simulate a reload after the mutable current cache was already poisoned. The independent
  // high-water snapshot must reconstruct the complete base; otherwise the next follow edit would
  // publish the short list and make the loss permanent.
  FOLLOWS = new Set();
  ClientSettings.set('followsCache', [person(0), person(1)]);
  globalThis.relayAnswer = kind3(2, 'short-after-reload');
  await fetchFollows();
  out.afterPoisonedReload = [...FOLLOWS].filter(p => p !== ME.pubkey).length;
  out.safetyCount = (ClientSettings.get('followsSafetyCache', [])||[]).length;

  // The own-profile header uses the same protected state, while another person's published list
  // remains their own and is never contaminated with our follows.
  out.ownProfile = _protectedProfileFollows(ME.pubkey, [person(0),person(1)]).length;
  out.otherProfile = _protectedProfileFollows(person(999), [person(0),person(1)]).length;

  // Even if both localStorage values were poisoned/cleared, the Store keeps replaceable versions
  // by event id. Recover from the largest historical kind-3 rather than blindly trusting newest.
  ClientSettings.set('followsCache', []);
  ClientSettings.set('followsSafetyCache', []);
  FOLLOWS = new Set();
  globalThis.storedEvents = [{...kind3(55,'older-good'), kind:3, pubkey:ME.pubkey},
                             {...kind3(2,'newer-bad'), kind:3, pubkey:ME.pubkey}];
  await fetchFollows();
  out.fromStoredHistory = [...FOLLOWS].filter(p => p !== ME.pubkey).length;

  // After an explicitly confirmed reset, an older large event replayed by another relay is history,
  // not authority. The reset timestamp prevents it from rehydrating the removed follows.
  ClientSettings.set('followsCache', Array.from({length:8},(_,i)=>person(i)));
  ClientSettings.set('followsSafetyCache', Array.from({length:8},(_,i)=>person(i)));
  ClientSettings.set('followsSafetyResetAt', 150);
  FOLLOWS = new Set();
  globalThis.storedEvents = [{...kind3(55,'concurrent-old-replay'), kind:3, pubkey:ME.pubkey, created_at:100},
                             {...kind3(8,'confirmed-new'), kind:3, pubkey:ME.pubkey, created_at:200}];
  globalThis.relayAnswer = {...kind3(8,'confirmed-new'), created_at:200};
  await fetchFollows();
  out.afterConcurrentOldReplay = [...FOLLOWS].filter(p => p !== ME.pubkey).length;
  return out;
})()`);

process.stdout.write(JSON.stringify(await run(kind3, person)));
