/* A SENT MESSAGE'S ECHO IS ACKNOWLEDGED ONCE, NOT ON EVERY READ OF THE ROOM.
 *
 * "communities is flashing now on desktop". Caught in the live desktop with a DOM breakpoint: the
 * message list was replaced 7 times in 10 s, every time by acknowledgeDeliveryEcho -> paintDelivery
 * -> backgroundRender, for a message that was already 'sent'. Runs the SHIPPED functions.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const code = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
function fn(header){
  const i = code.indexOf(header); assert(i >= 0, 'gone from concord.js: ' + header);
  let depth = 0;
  for (let k = code.indexOf('{', i); k < code.length; k++){
    if (code[k] === '{') depth++; else if (code[k] === '}' && --depth === 0) return code.slice(i, k + 1);
  }
}
const src = [fn('async function acknowledgeDeliveryEcho('), fn('function paintDelivery(')].join('\n');
const OWNER = 'a'.repeat(64);
const c = {
  window: {}, console, Set, Map,
  renders: 0, saves: 0,
  deliveries: new Map(),
  deliveryOwner: () => OWNER, roomIdentity: r => r.communityId, PC: () => ({}),
  envelopeCacheKey: () => 'k',
  persistDelivery: async () => { c.saves++; },
  testMessages: () => [{ id: 'rumor1', pubkey: OWNER }], saveTestMessages: () => {},
  backgroundRender: () => { c.renders++; },
};
vm.createContext(c);
vm.runInContext(src + '\nglobalThis.ack = acknowledgeDeliveryEcho;', c);
c.deliveries.set('d1', { owner: OWNER, roomId: 'room', channelId: 'gen', storeId: 's', status: 'sending',
  made: { wrap: { id: 'wrap1' }, rumorId: 'rumor1' } });
const room = { communityId: 'room' }, channel = { id: 'gen' };
const opened = { messages: [{ id: 'rumor1' }] }, wraps = [{ id: 'wrap1' }];

await c.ack({}, room, channel, wraps, opened);
assert.equal(c.renders, 1, 'the first echo must mark the message sent and repaint');
for (let i = 0; i < 9; i++) await c.ack({}, room, channel, wraps, opened);   // nine more reads of the room
assert.equal(c.renders, 1, `an already-sent message repainted the room ${c.renders} times over 10 reads`);
assert.equal(c.saves, 1, `an already-sent message was re-saved ${c.saves} times`);
console.log('OK a sent message is acknowledged once');
