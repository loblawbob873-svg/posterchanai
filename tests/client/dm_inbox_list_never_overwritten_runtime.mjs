/* A DM RELAY LIST SOMEBODY ALREADY HAS IS NEVER REPLACED.
 *
 *     "npub14w4q… is saying we changed his DM relays"
 *
 * Measured: his kind-10050 (relay.0xchat.com, relay.keychat.io, nostr21.com — signed 2026-05-29 by
 * another client) lived on his own relays. On 2026-09-19 a PosterChan client read the existing list
 * from its OWN pool, which never held it, merged our relay into nothing, and published a list naming
 * only wss://poster.place/relay to nostr21, ditto and purplepag.es. Runs the SHIPPED function.
 */
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { clientSourceAt } from './client_source.mjs';

const code = clientSourceAt(new URL('../../static/js/client/app.js', import.meta.url));
function fn(header){
  const i = code.indexOf(header); assert(i >= 0, 'gone: ' + header);
  let depth = 0;
  for (let k = code.indexOf('{', i); k < code.length; k++){
    if (code[k] === '{') depth++; else if (code[k] === '}' && --depth === 0) return code.slice(i, k + 1);
  }
}
const src = fn('async function ensureDmInboxList()');
const ME = 'ab'.repeat(32);
const THEIRS = { kind: 10050, pubkey: ME, id: 'old', created_at: 1780000000,
  tags: [['relay','wss://relay.0xchat.com'],['relay','wss://relay.keychat.io'],['relay','wss://nostr21.com']] };

async function run({ pool = [], external = [], answered = ['wss://purplepag.es/','wss://nostr21.com/'] }){
  const published = [];
  const c = {
    S: { GUEST: false, ME: { pubkey: ME } },
    DISCOVERY_RELAYS: ['wss://purplepag.es/', 'wss://user.kindpag.es/'],
    myInboxRelays: () => ['wss://poster.place/relay'],
    normalizeRelay: u => String(u || '').replace(/\/+$/, '') + '/',
    publish: async (kind, content, tags) => { published.push({ kind, tags }); return { ev: { kind, tags } }; },
    Relay: {
      query: async () => pool,
      queryFrom: async (urls, filters, opts) => { (opts.report.ok = answered.slice()); return external; },
      worker: { call: async (_m, { events }) => events.map(e => ({ id: e.id, valid: true })) },
      publishTo: async () => true,
    },
    Promise, Set, Array, String, Number, JSON, Object,
  };
  vm.createContext(c);
  vm.runInContext(src.replace('async function ensureDmInboxList', 'globalThis.ensure = async function') +
    '\nvar _dmInboxEnsured = false;', c);
  await c.ensure();
  return { published, retry: c._dmInboxEnsured === false };
}

/* 1. THE INCIDENT: our pool has nothing, his own relays have his list -> publish NOTHING. */
{ const r = await run({ external: [THEIRS] });
  assert.equal(r.published.length, 0, 'replaced a DM relay list that exists on the user\'s own relays: ' + JSON.stringify(r.published)); }

/* 2. Our pool already has his list -> publish nothing (and do not even add our relay to it). */
{ const r = await run({ pool: [THEIRS] });
  assert.equal(r.published.length, 0, 'edited an existing DM relay list'); }

/* 3. Nothing anywhere, and the relays really answered -> a FIRST list is created, naming our relay. */
{ const r = await run({});
  assert.equal(r.published.length, 1, 'a user with no DM relay list anywhere should get one');
  assert.equal(JSON.stringify(r.published[0].tags), JSON.stringify([['relay', 'wss://poster.place/relay']])); }

/* 4. Nothing found because nobody ANSWERED -> not evidence: publish nothing, retry later. */
{ const r = await run({ answered: ['wss://purplepag.es/'] });
  assert.equal(r.published.length, 0, 'published on the strength of a search almost nobody answered');
  assert.ok(r.retry, 'an unanswered search must leave the check for a later session'); }

console.log('OK an existing DM relay list is never replaced');
