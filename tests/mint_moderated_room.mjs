/* MINT A REAL COMMUNITY, POST AS TWO PEOPLE, AND HAVE A MODERATOR DELETE ONE OF THE MESSAGES.
 *
 * Everything runs the SHIPPED cord-protocol/cord-reader, on the bridge's own realm — the same
 * discipline as mint_concord_room.mjs, and for the same reason: a fixture that reimplements the
 * fold reproduces whatever the fold does wrong and calls it expected.
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
const spamSk  = NT.generateSecretKey(), spammer = NT.getPublicKey(spamSk);

const opts = toCord({ name: 'moderated room', icon: '', owner,
                      relays: ['wss://room.example'], base: 'https://poster.place' });
opts.signEvent = signer(ownerSk);
const made = await cord.PosterCord.createCommunity(opts);
const bundleEvents = made.events.filter(e => e.kind === 33301);
const controlWraps = made.events.filter(e => e.kind === 1059);
const opened = cord.PosterCord.openInvite(made.url, toCord(bundleEvents));
const info = cord.PosterCordReader.inspectControl(toCord(opened.bundle), toCord(controlWraps));
const channel = info.channels[0];

const say = async (sk, pk, text) => cord.PosterCordReader.createChatWrap(
  toCord(opened.bundle), toCord(controlWraps), channel.id, text, pk, signer(sk), toCord([]), 9);

/* The spam, and an ordinary message beside it so a test can tell "moderation worked" from
   "the whole channel stopped reading". */
const spam = await say(spamSk, spammer, 'BUY CHEAP FOLLOWERS http://spam.example');
const ok   = await say(ownerSk, owner, 'ordinary conversation');

/* THE OWNER REMOVES IT. A CORD deletion is a kind-5 naming the target rumor — exactly what
   concord.js publishes from its ⌫ button (`[['e',id],['k',String(kind)]]`, kind 5). */
const del = async (sk, pk, targetRumorId, targetKind) => cord.PosterCordReader.createChatWrap(
  toCord(opened.bundle), toCord(controlWraps), channel.id, '', pk, signer(sk),
  toCord([['e', targetRumorId], ['k', String(targetKind)]]), 5);

const ownerDelete   = await del(ownerSk, owner, spam.rumorId, 9);
/* …and the spammer trying to delete the OWNER's message, which must change nothing. */
const spammerDelete = await del(spamSk, spammer, ok.rumorId, 9);

console.log('ROOM ' + JSON.stringify({
  invite: made.url, ownerPk: owner, spammerPk: spammer,
  bundleEvents, controlWraps,
  channelId: channel.id, channelName: channel.name,
  moderators: info.moderators || [], ownerFromControl: info.owner || '',
  spamId: spam.rumorId, okId: ok.rumorId,
  wraps: [spam.wrap, ok.wrap],
  ownerDeleteWrap: ownerDelete.wrap,
  spammerDeleteWrap: spammerDelete.wrap,
}));
