/* THE SECOND DEVICE. Runs the SHIPPED decodeMembershipLists over a vault entry keyed by naddr --
   the shape a room joined from a plain invite link gets -- and checks the join bundle that comes
   out still carries its OWN community id.

   Reported live as: "Concord metadata sync failed Error: invalid Concord join material ... 
   inspectControl ... refreshRoomMetadata". The publishing device never sees it: its local row is
   kept as-is, and only a device rebuilding the room FROM the vault gets the corrupted bundle. */
import fs from 'node:fs';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');

function lift(header, endMarker){
  const a = src.indexOf(header);
  if (a < 0) throw new Error('moved: ' + header);
  const b = src.indexOf(endMarker, a);
  if (b < 0) throw new Error('end moved: ' + endMarker);
  return src.slice(a, b);
}
const code = lift('  function cordListHex(value){', '  /* Armada\'s membership `current` value')
           + lift('  function decodeMembershipLists(decrypted){', '\n  async function syncArmadaMemberships');
const { cordListHex, decodeMembershipLists } =
  new Function(`${code}; return {cordListHex, decodeMembershipLists};`)();

const HEX = 'ab'.repeat(32);                        // a real 32-byte community id
const NADDR = 'naddr1qvzqqqyzz5pzq3hjhx37t4u9uw5gthnmm3v62q3a5tqfxxqkze640quspp5rydekqqqqxw75uz';
const bundle = { community_id: HEX, owner: 'cd'.repeat(32), community_root: 'ef'.repeat(32),
                 channels: [{ name: 'general' }], name: 'PosterChan' };

// 1. keyed by naddr (an invite-link room): the bundle keeps its own id.
let out = decodeMembershipLists([{ event: { kind: 13302, tags: [] },
  doc: { entries: [{ community_id: NADDR, current: bundle, seed: bundle, invite_ref: 'https://x/invite/y#k' }],
         tombstones: [] } }]);
const e = out.entries[0];
if (e.current.community_id !== HEX)
  throw new Error(`bundle id overwritten with the vault key: ${e.current.community_id}`);
if (e.seed.community_id !== HEX)
  throw new Error(`seed id overwritten with the vault key: ${e.seed.community_id}`);
if (e.community_id !== NADDR)
  throw new Error(`the ENTRY must stay keyed by the naddr, got ${e.community_id}`);

// 2. keyed by a real community id: unchanged behaviour, the key is still applied as material.
const other = { ...bundle, community_id: 'ff'.repeat(32) };
out = decodeMembershipLists([{ event: { kind: 13302, tags: [] },
  doc: { entries: [{ community_id: HEX, current: other, seed: other }], tombstones: [] } }]);
if (out.entries[0].current.community_id !== HEX)
  throw new Error('a real community id must still be applied to the material');

// 3. a 43-char base64url key is a real id in fragment form and must still convert and apply.
const b64 = Buffer.from(HEX, 'hex').toString('base64url');
if (b64.length !== 43) throw new Error('fixture: expected a 43-char fragment key');
out = decodeMembershipLists([{ event: { kind: 13302, tags: [] },
  doc: { entries: [{ community_id: b64, current: other, seed: other }], tombstones: [] } }]);
if (out.entries[0].current.community_id !== HEX)
  throw new Error('a base64url community id must still convert and apply, got '
                  + out.entries[0].current.community_id);

console.log('concord vault key simulation: ok');
