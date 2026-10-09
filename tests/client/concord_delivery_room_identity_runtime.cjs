/* A queued Concord message still finds its room after the room's identity changes.
 * 2026-10-08: "Delivery needs attention: sending account or community changed" in the person's OWN room. A room
 * joined by link is identified by its url until the membership sync gives it a communityId; a message queued
 * before that matched no room afterwards, so Retry could never succeed. Runs the SHIPPED room-matching code. */
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../../static/js/client/concord.js'), 'utf8');
const grab = (name) => { const i = src.indexOf('  function ' + name + '('); assert.ok(i > 0, name); let d = 0, j = src.indexOf('{', i);
  for (; j < src.length; j++) { if (src[j] === '{') d++; else if (src[j] === '}' && --d === 0) break; } return src.slice(i, j + 1); };
const code = ['roomIdentity', 'communityKey', 'roomInviteKeys', 'sameRoom', 'deliveryAllowed'].map(grab).join('\n') + (src.includes('  function deliveryRoom(') ? '\n' + grab('deliveryRoom') : '');
let rooms = [];
const URL = 'https://poster.place/invite/naddr1abc#secret';
const ctx = { saved: () => rooms, deliveryOwner: () => 'me', inviteKey: r => (r && r.url ? String(r.url) : ''), String, window: {} };
vm.createContext(ctx);
vm.runInContext(code + ';globalThis.deliveryAllowed=deliveryAllowed;', ctx);
// Queued while the room was known only by its invite url…
const before = { url: URL, channels: [{ id: 'c1' }] };
rooms = [before];
const d = { owner: 'me', roomId: before.url, channelId: 'c1',
  roomRef: { url: URL, communityId: undefined, naddr: undefined, inviteAliases: undefined } };
assert.equal(!!ctx.deliveryAllowed({}, d), true, 'precondition: deliverable at first');
// …then the membership sync hydrated it with a communityId.
rooms = [{ communityId: 'adce738d0a99', url: URL, channels: [{ id: 'c1' }], cord: { bundle: { community_id: 'adce738d0a99' } } }];
assert.equal(!!ctx.deliveryAllowed({}, d), true, 'Retry after the room gained its community id said "community changed"');
// A room that really is gone, or a channel that was removed, still refuses.
rooms = [{ communityId: 'other', url: 'https://poster.place/invite/naddr1zzz#x', channels: [{ id: 'c1' }] }];
assert.equal(!!ctx.deliveryAllowed({}, d), false, 'a different room must not take the message');
rooms = [{ communityId: 'adce738d0a99', url: URL, channels: [{ id: 'c2' }] }];
assert.equal(!!ctx.deliveryAllowed({}, d), false, 'a removed channel must not take the message');
console.log('concord delivery: a queued message finds its room after the room gains its community id');
