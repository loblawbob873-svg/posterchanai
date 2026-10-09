/* A Concord member with no profile on this relay is looked up where the room lives -- once.
 * "why is vyram not displaying correctly" (2026-10-08): his room key a4c44c8e… had no kind-0 on poster.place
 * (not in its web of trust), so Concord showed a hex prefix. Runs the SHIPPED wantProfiles. */
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../../static/js/client/concord.js'), 'utf8');
const a = src.indexOf('  const profileAsked='), b = src.indexOf('\n  }\n', src.indexOf('  function wantProfiles(', a)) + 4;
assert.ok(a > 0 && b > a, 'wantProfiles not found');
const VYRAM = 'a4c44c8e0a6e8bc2c682cd7da00121a6dc22e63f518a50cb439f30f8c9987e88', KNOWN = 'b'.repeat(64), NOBODY = 'c'.repeat(64);
const queries = [], saved = []; let renders = 0;
const ROOM_RELAYS = ['wss://jskitty.com/nostr', 'wss://relay.ditto.pub'];
const ctx = { console, roomRelays: b => (b && b.relays) || [], setTimeout, Set, Map, Promise, String, window: { Store: { saveProfile: e => saved.push(e.pubkey) } },
  cordQuery: async (p, relays, filters) => { queries.push({ relays, authors: filters[0].authors });
    return filters[0].authors.includes(VYRAM) ? [{ kind: 0, pubkey: VYRAM, content: '{"name":"Vyram Kraven"}' }] : []; },
  backgroundRender: () => { renders++; } };
vm.createContext(ctx);
vm.runInContext(src.slice(a, b) + ';globalThis.wantProfiles=wantProfiles;', ctx);
const p = { profOf: pk => pk === KNOWN ? { name: 'Known' } : null };
const room = { cord: { bundle: { relays: ROOM_RELAYS } } };
(async () => {
  ctx.wantProfiles(p, [VYRAM, KNOWN, NOBODY, 'not-a-key'], room);
  ctx.wantProfiles(p, [VYRAM], room);                       // a repaint asking again before the batch runs
  await new Promise(r => setTimeout(r, 400));
  assert.equal(queries.length, 1, 'the missing members were not batched into one lookup');
  assert.equal(JSON.stringify([...queries[0].authors].sort()), JSON.stringify([VYRAM, NOBODY].sort()), 'a member who already has a name was looked up');
  assert.equal(JSON.stringify(queries[0].relays), JSON.stringify(ROOM_RELAYS),
    'members must be looked up on the room\'s own relays and nowhere else (the batch is the member list)');
  assert.equal(JSON.stringify(saved), JSON.stringify([VYRAM])); assert.equal(renders, 1, 'a found profile must repaint the room');
  ctx.wantProfiles(p, [VYRAM, NOBODY], room);              // later repaints: asked once per session, not nagged
  await new Promise(r => setTimeout(r, 400));
  assert.equal(queries.length, 1, 'a member was asked again');
  console.log('concord member profiles: missing names looked up once, from the room\'s own relays');
})().catch(e => { console.error(e); process.exit(1); });
