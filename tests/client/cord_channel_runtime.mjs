/* RUN the shipped CORD implementation: mint a community, add channels, read them back.
 *
 * FOUR REALM BOUNDARIES MAKE THIS HARD, and every one of them fails as "can't serialize event with
 * wrong or missing properties" or "expected Uint8Array, got object" — errors that look like the
 * library is broken when the harness is. A `vm` context has ECMAScript intrinsics but none of
 * Node's globals, so:
 *
 *   * do NOT inject the host `Object`/`Array`/`JSON` — an object literal built inside the vm gets
 *     the VM's Object.prototype while `Object` would name the HOST one, so every prototype check
 *     inside the bundle silently fails;
 *   * `TextEncoder` must return a realm-native Uint8Array, or noble's `instanceof` check sees a
 *     plain object;
 *   * `crypto.getRandomValues` must fill the CALLER's array for the mirror-image reason;
 *   * the SIGNED EVENT handed back by the signer has to be rebuilt in the bundle's realm too.
 *
 * Each shim crosses the boundary as a plain Array/string and is rebuilt on the far side.
 */
import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

const ROOT = new URL('../../', import.meta.url);

function realm() {
  const ctx = vm.createContext({
    console,
    __enc: (s) => Array.from(new TextEncoder().encode(String(s))),
    __dec: (a) => new TextDecoder().decode(Uint8Array.from(a)),
    __rand: (n) => Array.from(webcrypto.getRandomValues(new Uint8Array(n))),
    __uuid: () => webcrypto.randomUUID(),
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
  `, ctx);
  return ctx;
}

const load = (ctx, rel) => vm.runInContext(
  fs.readFileSync(new URL(rel, ROOT), 'utf8'), ctx, { filename: rel });

export async function community() {
  const nt = realm();
  load(nt, 'static/vendor/nostr/nostr.bundle.js');
  const cord = realm();
  load(cord, 'static/js/client/cord-protocol.js');
  load(cord, 'static/js/client/cord-reader.js');

  const NT = nt.NostrTools, R = cord.PosterCordReader;
  const into = (c) => (v) => vm.runInContext('(' + JSON.stringify(v) + ')', c);
  const toCord = into(cord), toNT = into(nt);
  const sk = NT.generateSecretKey(), pk = NT.getPublicKey(sk);
  const signEvent = (t) => toCord(NT.finalizeEvent(toNT({
    kind: t.kind, created_at: t.created_at ?? Math.floor(Date.now() / 1000),
    tags: t.tags || [], content: t.content ?? '' }), sk));

  const opts = toCord({ name: 'probe', icon: '', owner: pk,
                        relays: ['wss://relay.example'], base: 'https://poster.place' });
  opts.signEvent = signEvent;
  const made = await cord.PosterCord.createCommunity(opts);
  const bundle = toCord({ community_id: made.communityId, owner: pk,
    owner_salt: made.secrets.ownerSalt, community_root: made.secrets.root, root_epoch: 0,
    channels: [], relays: ['wss://relay.example'], name: 'probe', creator_npub: pk });
  let wraps = made.events.filter((e) => e.kind === 1059);

  return {
    owner: pk,
    stranger: () => NT.getPublicKey(NT.generateSecretKey()),
    channels: () => R.inspectControl(bundle, wraps).channels
                     .map((c) => ({ name: c.name, id: c.id, private: c.private })),
    /** Create one, and KEEP its wrap — a later read must still see every earlier channel. */
    add: async (spec) => {
      const made2 = await R.createChannelWrap(bundle, wraps, toCord(spec), pk, signEvent);
      wraps = wraps.concat([made2.wrap]);
      return { id: made2.channelId, name: made2.name, kind: made2.wrap.kind };
    },
    /** Attempt one WITHOUT keeping the result — for the cases that must be refused. */
    attempt: (spec, who) => R.createChannelWrap(bundle, wraps, toCord(spec), who || pk, signEvent),
  };
}
