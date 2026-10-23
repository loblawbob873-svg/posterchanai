// WHICH CALL SERVER a Concord call uses, decided by the SHIPPED code: cord-protocol.js + cord-reader.js mint a real
// community (PosterCord.createCommunity, the writer the client and tests/client/concord_fixture_relay.py MINT use),
// cord-reader.js folds its metadata, and cord-voice.js picks the broker the call panel tries.
//
// Why: the live interop test of 2026-10-22 (scripts/check_armada_call_interop.py) split whenever PosterChan joined
// first -- we took this instance's broker for an empty room, Armada took the community's declared `av_brokers`
// (Concord PR #22) and never follows presence. Now a declared list is the ONLY candidate set, and a community made
// here declares this instance's broker in Armada's exact field.
import assert from 'node:assert/strict';
import { webcrypto, createHash } from 'node:crypto';
import { makeRealm, loadInto, into } from '../../botframework/cord_realm.mjs';

const nt = makeRealm(); loadInto(nt, 'static/vendor/nostr/nostr.bundle.js');
const cord = makeRealm();
loadInto(cord, 'static/js/client/cord-protocol.js'); loadInto(cord, 'static/js/client/cord-reader.js');
cord.crypto.subtle = webcrypto.subtle;
loadInto(cord, 'static/js/client/cord-voice.js');
const NT = nt.NostrTools, toCord = into(cord), toNT = into(nt), R = cord.PosterCordReader, V = cord.PCCordVoice;
const plain = (v) => JSON.parse(JSON.stringify(v));
const signer = (sk) => (t) => toCord(NT.finalizeEvent(toNT({ kind: t.kind, created_at: t.created_at ?? Math.floor(Date.now() / 1000),
  tags: t.tags || [], content: t.content ?? '' }), sk));
const ownerSk = NT.generateSecretKey(), owner = NT.getPublicKey(ownerSk);

async function mint(extra) {
  const opts = toCord({ name: 'brokers', icon: '', owner, relays: ['wss://room.example'], base: 'https://poster.place', ...extra });
  opts.signEvent = signer(ownerSk);
  const made = await cord.PosterCord.createCommunity(opts);
  const opened = cord.PosterCord.openInvite(made.url, toCord(made.events.filter(e => e.kind === 33301)));
  const controls = toCord(made.events.filter(e => e.kind === 1059));
  const info = R.inspectControl(toCord(opened.bundle), controls);
  const room = R.voiceMaterial(toCord(opened.bundle), controls, info.channels[0].id).room;
  return { info, room, made };
}
const rank = (room, o) => createHash('sha256').update(Buffer.concat([Buffer.from(room, 'hex'), Buffer.from(o)])).digest('hex');
const PC = 'https://poster.place', ARM = 'https://av.armada.buzz';

// 1. A community CREATED here declares this instance's broker, in Armada's wire form: `av_brokers`, an array of
//    canonical https origins in the CORD-02 metadata entity.
const declaredHere = await mint({ avBrokers: ['https://poster.place/', 'http://insecure.example', 'https://u:p@creds.example'] });
assert.deepEqual(plain(declaredHere.info.av_brokers), [PC], 'a community made here must declare this instance as its call server');
// (The reader reads `folded.metadata.av_brokers` -- the parsed genesis edition -- so this value round-tripped through
// that exact key, the one Armada's communityAvBrokers reads.)

// 2. Nothing given (no instance / no SFU): nothing declared.
const bare = await mint({});
assert.deepEqual(plain(bare.info.av_brokers), [], 'with no instance broker nothing is declared');

// 3. The reader normalises what OTHER clients wrote exactly like Armada's canonicalOrigin + cap of 5.
assert.deepEqual(plain(V.declared(toCord(['HTTPS://AV.Armada.buzz:443/x', 'https://av.armada.buzz', 'http://plain.example', 5,
  'https://a.example', 'https://b.example', 'https://c.example', 'https://d.example', 'https://e.example']))),
  [ARM, 'https://a.example', 'https://b.example', 'https://c.example', 'https://d.example']);

// A room where the CORD-07 tie-break favours poster.place -- the exact shape of the 2026-10-22 split.
let room = declaredHere.room;
for (let i = 0; rank(room, PC) > rank(room, ARM) && i < 64; i++) room = (await mint({})).room;
assert.ok(rank(room, PC) < rank(room, ARM), 'fixture: need a room whose tie-break favours poster.place');

// 4. DECLARED LIST (an Armada-made community: av_brokers=[av.armada.buzz]): only that server, whichever side joins
//    first -- an empty room, a PosterChan member already on poster.place, or Armada already there.
for (const occupied of [[], [PC], [ARM], [PC, ARM]])
  assert.deepEqual(plain(await V.candidates(room, toCord({ declared: [ARM], occupied, preferred: PC }))), [ARM],
    `declared [armada], occupied ${JSON.stringify(occupied)}: must use only the declared server`);
// several declared: occupied-and-listed first (CORD-07 §5 agreeing with the list), then the list in tie-break order
assert.deepEqual(plain(await V.candidates(room, toCord({ declared: [ARM, PC], occupied: [ARM], preferred: '' }))), [ARM, PC]);
assert.deepEqual(plain(await V.candidates(room, toCord({ declared: [ARM, PC], occupied: [], preferred: '' }))), [PC, ARM]);
// healing stays inside the list: presence on an undeclared broker never moves the call
assert.equal(await V.heal(room, ARM, toCord([PC]), toCord([ARM])), ARM);

// 5. NOTHING DECLARED: unchanged -- occupied brokers first, then this instance's; §5 healing toward the winner.
assert.deepEqual(plain(await V.candidates(room, toCord({ declared: [], occupied: [], preferred: PC }))), [PC]);
assert.deepEqual(plain(await V.candidates(room, toCord({ declared: [], occupied: [ARM], preferred: PC }))), [ARM, PC]);
assert.equal(await V.heal(room, ARM, toCord([PC]), toCord([])), PC);

// 6. A community made HERE, called from here: its declared broker is the one used even if the browser remembered
//    another, and an Armada member's presence on av.armada.buzz does not pull us off it.
assert.deepEqual(plain(await V.candidates(declaredHere.room, toCord({ declared: plain(declaredHere.info.av_brokers), occupied: [ARM], preferred: 'https://elsewhere.example' }))), [PC]);
console.log('OK declared call servers');
