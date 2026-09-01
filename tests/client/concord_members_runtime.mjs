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

console.log('ok');
