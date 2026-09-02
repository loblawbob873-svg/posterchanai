/* @-MENTION CANNOT COMPLETE SOMEBODY THE ROOM WILL NOT NAME.
 *
 * Reported as "Concord: user tagging still not working, I want to @ tab autocomplete and it
 * notifies the user properly". Tab was already bound and the `p`/`P` tags were already published —
 * both correct. The candidate list was the problem: `roomParticipants` read MESSAGE AUTHORS and
 * nothing else, so a member who had not posted (or whose posts were not in the loaded history) did
 * not exist as far as the autocomplete was concerned. There was nobody to complete to.
 *
 * The room's control document knows: `controlPubkeys` are its admins, and each channel's
 * `streamPubkeys` are the keys allowed to write there. This drives the real function.
 */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const store = {};
const localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
const document = {querySelector:()=>null, querySelectorAll:()=>[], createElement:()=>({dataset:{}}),
  head:{appendChild:noop}, documentElement:{appendChild:noop}, addEventListener:noop,
  body:{classList:{add:noop, remove:noop, contains:()=>false}}};
const window = {document, addEventListener:noop};
vm.runInNewContext(src, {window, document, console, setTimeout:()=>0, clearTimeout:noop,
  URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

const ME = 'a'.repeat(64), TALKER = 'b'.repeat(64), QUIET = 'c'.repeat(64), ADMIN = 'd'.repeat(64);
const ROOM = {communityId:'cid-1', channels:[{name:'general', id:'c1'}],
              cord:{bundle:{fake:true}}};

/* A member who has posted: the store is what `testMessages` reads. */
localStorage.setItem('pc.concord.test.cid-1',
  JSON.stringify([{id:'m1', pubkey:TALKER, text:'hi', at:1, kind:9, tags:[]}]));

const before = api.roomParticipants(ROOM, ME);
if (!before.includes(TALKER)) throw new Error('a member who posted is missing: ' + JSON.stringify(before));

/* THE ROOM'S OWN MEMBER LIST. Nobody here has posted; before this change none of them could be
   @-mentioned, shown in Members, or called. */
window.PosterCordReader = {
  inspectControl: () => ({ controlPubkeys:[ADMIN],
                           channels:[{id:'c1', name:'general', streamPubkeys:[QUIET, TALKER]}] }),
};
const after = api.roomParticipants(ROOM, ME);
for (const [who, pk] of [['the viewer', ME], ['a member who posted', TALKER],
                         ['a member who has never posted', QUIET], ['an admin', ADMIN]]) {
  if (!after.includes(pk)) throw new Error(who + ' is not offerable: ' + JSON.stringify(after));
}
if (new Set(after).size !== after.length) throw new Error('duplicates: ' + JSON.stringify(after));

/* A ROOM WHOSE CONTROL VIEW THROWS must still name the people visibly talking in it — losing them
   would be a worse bug than the one being fixed. */
window.PosterCordReader = { inspectControl: () => { throw new Error('not decrypted yet'); } };
const broken = api.roomParticipants({...ROOM, communityId:'cid-2'}, ME);
if (!broken.includes(ME)) throw new Error('the viewer vanished when the control view threw');

/* NO READER AT ALL — an older build, or the libs not loaded yet. */
delete window.PosterCordReader;
const bare = api.roomParticipants({...ROOM, communityId:'cid-3'}, ME);
if (!bare.includes(ME)) throw new Error('no reader means no participants at all');

/* THE LAYER BETWEEN THE LIST AND THE TAGS.
 *
 * Having the right candidates is not the same as tagging the right person. The picker labels a
 * choice `display_name || name || pubkey.slice(0,12)` and writes THAT into the box with spaces as
 * underscores; `typedMentionRecipients` has to resolve the identical text back to a pubkey, or
 * tab-completing tags somebody and typing the same characters by hand tags nobody — silently,
 * which is how this was reported the first time. Both call sites, one idea of what a person is
 * called. */
window.PosterCordReader = {
  inspectControl: () => ({ controlPubkeys:[ADMIN],
                           channels:[{id:'c1', name:'general', streamPubkeys:[QUIET, TALKER]}] }),
};
const ROOM4 = {...ROOM, communityId:'cid-4'};
const parts = api.roomParticipants(ROOM4, ME);
const NAMED = 'e'.repeat(64);
const profiles = { [NAMED]: {display_name:'Ada Lovelace'} };
const profOf = pk => profiles[pk] || {};

/* A member with a real name: the picker writes "@Ada_Lovelace". */
const byName = api.typedMentionRecipients('hey @Ada_Lovelace look', [...parts, NAMED], profOf);
if (!byName.includes(NAMED)) throw new Error('a typed display name tags nobody: ' + JSON.stringify(byName));

/* A member with NO profile: the picker's own label is a 12-character pubkey prefix, so that is what
   the user sees and can retype. This is the exact case that used to resolve to nothing. */
const byPrefix = api.typedMentionRecipients('ping @' + QUIET.slice(0,12), parts, profOf);
if (!byPrefix.includes(QUIET)) throw new Error('a typed pubkey-prefix handle tags nobody: ' + JSON.stringify(byPrefix));

/* Somebody who is not in the room must not be taggable by guessing a name. */
const stranger = api.typedMentionRecipients('hi @Ada_Lovelace', parts, profOf);
if (stranger.length) throw new Error('tagged a non-member: ' + JSON.stringify(stranger));

/* No @ at all means no tags — the send path builds P/p tags from exactly this. */
if (api.typedMentionRecipients('no mentions here', parts, profOf).length)
  throw new Error('tagged somebody with no @ in the text');


/* ---- THE MENTION HAS TO BE VISIBLE, not just published --------------------------------------
 * Reported as "its not tagging users in concord right so they get the mention or highlight like
 * armada does it". The p/P tags were always correct; the only thing that consumed them was an OS
 * notification, so a message tagging you looked like every other message in the channel. */
const paint = api.paintMentions, mentionsMe = api.messageMentionsViewer;
if (!paint || !mentionsMe) throw new Error('the mention helpers are not exported');

const painted = paint('hey @Ada_Lovelace look', ['Ada Lovelace']);
if (!/class="cc-mention cc-mention-me"/.test(painted)) throw new Error('my own mention is not marked: ' + painted);
const other = paint('hey @someone_else look', ['Ada Lovelace']);
if (!/class="cc-mention"/.test(other)) throw new Error('a mention of someone else is not painted: ' + other);
if (/cc-mention-me/.test(other)) throw new Error('somebody else’s mention is marked as mine: ' + other);

/* A handle inside a URL is not a mention — the @ there is preceded by a slash. */
const url = paint('see https://x.com/@someone for more', ['Ada Lovelace']);
if (/cc-mention/.test(url)) throw new Error('painted a mention inside a url: ' + url);

/* It runs over ALREADY-ESCAPED html and must not be able to introduce markup. */
const inert = paint('&lt;img src=x onerror=1&gt; @Ada_Lovelace', ['Ada Lovelace']);
if (/<img/i.test(inert)) throw new Error('the mention pass unescaped markup: ' + inert);

const VIEWER = {pubkey: ME, profile:{display_name:'Ada Lovelace'}, npub:'npub1ada'};
if (!mentionsMe({pubkey:TALKER, text:'hi', tags:[['p',ME]]}, VIEWER, 'Ada Lovelace'))
  throw new Error('a p-tagged message does not count as a mention');
if (!mentionsMe({pubkey:TALKER, text:'hi @Ada_Lovelace', tags:[]}, VIEWER, 'Ada Lovelace'))
  throw new Error('a typed handle does not count as a mention');
if (mentionsMe({pubkey:ME, text:'hi @Ada_Lovelace', tags:[['p',ME]]}, VIEWER, 'Ada Lovelace'))
  throw new Error('my own message counts as a mention of me');
if (mentionsMe({pubkey:TALKER, text:'nothing here', tags:[['p',QUIET]]}, VIEWER, 'Ada Lovelace'))
  throw new Error('a message tagging somebody else counts as mine');

/* THE WIRING, NOT THE HELPER. Testing `paintMentions` directly says nothing about whether the
 * message renderer calls it — a mutation that removed it from `messageContentHtml` passed every
 * check above, which is exactly the shape of test that lets a bug ship. Render a real message. */
const PC_STUB = {
  enc: v => String(v==null?'':v).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  linkify: t => PC_STUB.enc(t),
  viewer: () => ({pubkey: ME, profile:{display_name:'Ada Lovelace'}, npub:'npub1ada'}),
  profOf: () => ({}),
};
const rendered = api.messageContentHtml(PC_STUB,
  {id:'m9', pubkey:TALKER, text:'hey @Ada_Lovelace and @someone_else', at:1, kind:9, tags:[]},
  ROOM4, 'general');
if (!/class="cc-mention cc-mention-me"/.test(rendered))
  throw new Error('the rendered message does not mark the reader\u2019s own mention: ' + rendered);
if (!/class="cc-mention"/.test(rendered))
  throw new Error('the rendered message paints no mention at all: ' + rendered);

/* And the ROW must carry the class the stylesheet tints — the Armada behaviour is that a message
 * which tags you LOOKS different, not merely that one word inside it does. */
if (!/cc-mentions-me/.test(src) || !/messageMentionsViewer\(m,viewer,me\)/.test(src))
  throw new Error('the message row no longer marks itself when it mentions the reader');

console.log('ok');
