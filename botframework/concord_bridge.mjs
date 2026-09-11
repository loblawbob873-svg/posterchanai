/* THE ONE CORD IMPLEMENTATION, REACHABLE FROM PYTHON.
 *
 * A Concord room is not a Nostr post: the bundle is opened from an invite whose `#` fragment is the
 * decryption secret, and every message is a CORD gift wrap on the ROOM's own relays. All of that
 * logic lives in the shipped `cord-protocol.js` / `cord-reader.js`, which the web client uses — so
 * this drives THOSE rather than porting the protocol into Python. A second implementation of a wire
 * format is a second thing to drift, and this repo has paid for that before.
 *
 * Node does the crypto; Python keeps the relays, the config and the bot's own logic.
 *
 * REALM DISCIPLINE (four boundaries, each of which fails as a misleading error):
 * a `vm` context has ECMAScript intrinsics but none of Node's globals, and supplying the HOST ones
 * is the trap — `new TextEncoder().encode()` then returns a host Uint8Array and noble's
 * `instanceof` check inside the vm sees a plain object ("expected Uint8Array, got object"), while
 * an object literal built in the vm gets the VM's Object.prototype and every prototype check fails
 * ("can't serialize event with wrong or missing properties"). Each shim crosses as a plain
 * Array/string and is rebuilt on the far side.
 *
 * Protocol: one JSON request per line on stdin, one JSON response per line on stdout.
 */
import fs from 'node:fs';
import vm from 'node:vm';
import readline from 'node:readline';
import { webcrypto } from 'node:crypto';

const ROOT = new URL('../', import.meta.url);

function realm() {
  const ctx = vm.createContext({
    __enc: (s) => Array.from(new TextEncoder().encode(String(s))),
    __dec: (a) => new TextDecoder().decode(Uint8Array.from(a)),
    __rand: (n) => Array.from(webcrypto.getRandomValues(new Uint8Array(n))),
    __uuid: () => webcrypto.randomUUID(),
    __now: () => Date.now(),
    __btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    __atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    setTimeout, clearTimeout,
  });
  vm.runInContext(`
    globalThis.TextEncoder = class TextEncoder { encode(s){ return Uint8Array.from(__enc(s)); } };
    globalThis.TextDecoder = class TextDecoder { decode(b){ return __dec(Array.from(b ?? [])); } };
    globalThis.crypto = { getRandomValues(a){ a.set(__rand(a.length)); return a; },
                          randomUUID: () => __uuid() };
    globalThis.btoa = (s) => __btoa(String(s));
    globalThis.atob = (s) => __atob(String(s));
    globalThis.window = globalThis; globalThis.self = globalThis;
    globalThis.document = { createElement: () => ({}), querySelector: () => null };
    globalThis.location = { origin: 'https://poster.place' };
    /* performance.now is a Node global, not an ECMAScript intrinsic, so a bare vm realm has
       none - and the reader times its decrypt batches with it. Missing, the read op failed with
       "performance is not defined" while open and say both worked, which reads as the message
       being unreadable rather than the realm being incomplete.
       (No backticks in this comment: it is inside a template literal, and one would end it.) */
    globalThis.performance = { now: () => __now() };
    globalThis.console = { log(){}, warn(){}, error(){} };   // stdout is the protocol
  `, ctx);
  return ctx;
}
const load = (ctx, rel) => vm.runInContext(fs.readFileSync(new URL(rel, ROOT), 'utf8'), ctx,
                                           { filename: rel });

const nt = realm();   load(nt, 'static/vendor/nostr/nostr.bundle.js');
const cord = realm(); load(cord, 'static/js/client/cord-protocol.js');
                      load(cord, 'static/js/client/cord-reader.js');
const NT = nt.NostrTools, R = cord.PosterCordReader;
const into = (c) => (v) => vm.runInContext('(' + JSON.stringify(v) + ')', c);
const toCord = into(cord), toNT = into(nt);

const signerFor = (nsec) => {
  const sk = /^[0-9a-f]{64}$/i.test(nsec) ? Uint8Array.from(Buffer.from(nsec, 'hex'))
                                          : NT.nip19.decode(nsec).data;
  const skNT = vm.runInContext('new Uint8Array(' + JSON.stringify([...sk]) + ')', nt);
  return {
    pubkey: NT.getPublicKey(skNT),
    sign: (t) => toCord(NT.finalizeEvent(toNT({
      kind: t.kind, created_at: t.created_at ?? Math.floor(Date.now() / 1000),
      tags: t.tags || [], content: t.content ?? '' }), skNT)),
  };
};

/* Each op is deliberately PURE: it takes the events Python fetched and returns events for Python to
 * publish. The bridge opens no sockets — relays, retries and rate limits stay in one place. */
const ops = {
  /** What relays to query, and who signs the bundle, for an invite URL. */
  inviteDetails: ({ url }) => cord.PosterCord.inviteDetails(url),

  /** Open the invite against the 33301 events Python fetched from those relays. */
  openInvite: ({ url, events }) => {
    const opened = cord.PosterCord.openInvite(url, toCord(events || []));
    return { bundle: opened.bundle, communityId: opened.bundle.community_id,
             name: opened.bundle.name || '', relays: opened.bundle.relays || [] };
  },

  /** The channels and members this bundle can see, given its control wraps. */
  inspect: ({ bundle, controlWraps }) => {
    const info = R.inspectControl(toCord(bundle), toCord(controlWraps || []));
    return { channels: info.channels, members: info.members, name: info.name };
  },

  /** Decrypt a channel's messages. */
  read: ({ bundle, controlWraps, channelId, chatWraps }) =>
    R.inspectChat(toCord(bundle), toCord(controlWraps || []), channelId, toCord(chatWraps || [])),

  /** A message to publish to the room's relays. */
  say: async ({ bundle, controlWraps, channelId, text, nsec, tags, kind }) => {
    const signer = signerFor(nsec);
    const made = await R.createChatWrap(toCord(bundle), toCord(controlWraps || []), channelId,
                                        String(text), signer.pubkey, signer.sign,
                                        toCord(tags || []), kind || 9);
    return { rumorId: made.rumorId, wrap: made.wrap };
  },

  /** The NIP-42 auth event a CORD relay demands for this plane. */
  planeAuth: async ({ bundle, controlWraps, challenge, relay, nsec }) => {
    const signer = signerFor(nsec);
    return R.createPlaneAuth(toCord(bundle), toCord(controlWraps || []),
                             { challenge, relay }, signer.pubkey, signer.sign);
  },
};

const out = (v) => process.stdout.write(JSON.stringify(v) + '\n');
readline.createInterface({ input: process.stdin }).on('line', async (line) => {
  if (!line.trim()) return;
  let req;
  try { req = JSON.parse(line); } catch (e) { return out({ ok: false, error: 'bad request json' }); }
  const fn = ops[req && req.op];
  if (!fn) return out({ id: req && req.id, ok: false, error: 'unknown op: ' + (req && req.op) });
  try { out({ id: req.id, ok: true, result: await fn(req) }); }
  catch (e) { out({ id: req.id, ok: false, error: String((e && e.message) || e) }); }
});
