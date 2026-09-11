/* MINT A REAL CONCORD COMMUNITY, so the bot framework can be tested against the thing itself.
 *
 * Everything here runs the SHIPPED cord-protocol/cord-reader: a community, its invite URL, its
 * bundle events, its control wraps, and two chat messages — one that names a bot and one that does
 * not. A bot test that mocks this is a test of the mock; the only question worth asking is whether
 * what the bridge produces is what the reader (and therefore Armada) accepts.
 *
 * IT IMPORTS THE BRIDGE'S OWN REALM. It used to build its own, and that is how a missing `URL`
 * stayed invisible: the invite parser swallows the throw and answers "invalid CORD invite", so a
 * fixture with the same gap reproduces the bug and calls it expected. One definition, in
 * botframework/cord_realm.mjs.
 *
 * Prints one line: ROOM <json>.
 */
import { makeRealm, loadInto, into } from '../botframework/cord_realm.mjs';

const nt = makeRealm();   loadInto(nt, 'static/vendor/nostr/nostr.bundle.js');
const cord = makeRealm(); loadInto(cord, 'static/js/client/cord-protocol.js');
                          loadInto(cord, 'static/js/client/cord-reader.js');
const NT = nt.NostrTools, toCord = into(cord), toNT = into(nt);
const signer = (sk) => (t) => toCord(NT.finalizeEvent(toNT({
  kind: t.kind, created_at: t.created_at ?? Math.floor(Date.now() / 1000),
  tags: t.tags || [], content: t.content ?? '' }), sk));

const ownerSk = NT.generateSecretKey(), owner = NT.getPublicKey(ownerSk);
const botSk = NT.generateSecretKey(), bot = NT.getPublicKey(botSk);

const opts = toCord({ name: 'bot room', icon: '', owner,
                      relays: ['wss://room.example'], base: 'https://poster.place' });
opts.signEvent = signer(ownerSk);
const made = await cord.PosterCord.createCommunity(opts);
const bundleEvents = made.events.filter(e => e.kind === 33301);
const controlWraps = made.events.filter(e => e.kind === 1059);

// Open it the way a bot does: from the invite URL alone.
const details = cord.PosterCord.inviteDetails(made.url);
const opened = cord.PosterCord.openInvite(made.url, toCord(bundleEvents));
const info = cord.PosterCordReader.inspectControl(toCord(opened.bundle), toCord(controlWraps));
const channel = info.channels[0];

const botNpub = NT.nip19.npubEncode(bot);
const chat = async (text) => (await cord.PosterCordReader.createChatWrap(
  toCord(opened.bundle), toCord(controlWraps), channel.id, text, owner,
  signer(ownerSk), toCord([]), 9)).wrap;

console.log('ROOM ' + JSON.stringify({
  invite: made.url,
  botNsec: NT.nip19.nsecEncode(botSk), botNpub, botPk: bot, ownerPk: owner,
  bundleEvents, controlWraps,
  bootstrapRelays: details.bootstrapRelays, linkSigner: details.linkSigner,
  roomRelays: opened.bundle.relays,
  channelId: channel.id, channelName: channel.name, streamPubkeys: channel.streamPubkeys,
  controlPubkeys: info.controlPubkeys,
  mentionWrap: await chat('hey ' + botNpub + ' what do you think?'),
  chatterWrap: await chat('unrelated chatter in the room'),
}));
