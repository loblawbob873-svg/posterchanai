/* Run the SHIPPED startChatLive against a stubbed Relay and record how it subscribes.
 *
 * The reconnect lives in relay.js, but whether a Concord room GETS it is one word at this call
 * site. Losing that word restores the original bug in full and changes nothing a reader would
 * notice, so it is asserted by running the function, not by looking for the string. */
import fs from 'node:fs';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const a = src.indexOf('  function startChatLive(p,room,channel){');
if (a < 0) throw new Error('startChatLive moved');
const b = src.indexOf('\n  }\n', a) + 4;
const body = src.slice(a, b);

const calls = [];
const Relay = {
  subscribe: (filters, opts) => { calls.push({ how: 'subscribe', filters, opts: { ...opts, onEvent: undefined } }); return 'p1'; },
  subscribeFrom: (urls, filters, opts) => {
    calls.push({ how: 'subscribeFrom', urls, filters, opts: { ...opts, onEvent: undefined } });
    const stop = () => {}; stop.hasTargets = true; stop.ready = Promise.resolve(true); return stop;
  },
  close: () => {},
};
const room = { communityId: 'c1', url: 'https://x/i', cord: { bundle: { relays: ['wss://room.example'] } } };
const channel = { id: 'ch1', name: 'general', streamPubkeys: ['aa'.repeat(32)] };

/* The module-level state startChatLive mutates, declared inside the same scope the extracted
   function is compiled into — the real file has them a few lines above it. */
const state = "let chatSub=null,chatSubKey='',chatBuffer=[],chatFlush=null;\n";
const fn = new Function(
  'window', 'roomIdentity', 'stopChatLive', 'roomRelays', 'flushChatLive', 'console',
  'setTimeout', 'Math', 'Date', 'Number', 'String',
  state + body + '\nreturn startChatLive;')(
    { Relay }, (r) => String(r && r.communityId || ''), () => {},
    (bundle) => (bundle && bundle.relays) || [], () => {}, console,
    setTimeout, Math, Date, Number, String);

/* The extracted function closes over its own copies of the module-level state, which is exactly
   what is wanted here: this measures one arming, not the latch. */
fn({ viewer: () => ({}) }, room, channel);
process.stdout.write(JSON.stringify({ calls }));
