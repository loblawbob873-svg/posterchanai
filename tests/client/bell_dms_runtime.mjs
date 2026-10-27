/* A DM LIGHTS THE BELL IN DESKTOP MODE AND POSTERCHANOS, AND ONLY THERE (2026-10-27: "maybe for Desktop mode and OS
 * mode, light the bell"; the 10-11 rule -- a DM badges Messages only -- still holds in the web/phone layout).
 *
 * In the desktop the bell counts notifications + unread DMs that arrived since Notifications was last opened;
 * Messages keeps its own full count until they are read. Runs the SHIPPED notifs.js notifUnread and _desktopBell and
 * the SHIPPED dms.js dmUnreadSince with stand-ins for their state. */
import fs from 'fs';
const src = f => fs.readFileSync(new URL('../../static/js/client/' + f, import.meta.url), 'utf8');
const slice = (text, start) => { const i = text.indexOf(start); if (i < 0) throw new Error('missing ' + start);
  let d = 0, j = text.indexOf('{', i); for (let k = j; k < text.length; k++) { d += text[k] === '{'; d -= text[k] === '}'; if (!d) return text.slice(i, k + 1); } };
const bellFn = slice(src('notifs.js'), 'function _desktopBell(') + '\n' + slice(src('notifs.js'), 'function notifUnread(');
const dmFn = slice(src('dms.js'), 'function dmUnreadSince(');
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

const S = { ME: { pubkey: 'me' }, _dmUnread: 3, _newBuild: false, _apkUpdate: false, _updBadge: false, _notifEpoch: 0 };
const seenNotif = { last: 0 };
// Three unread DMs from alice (t=100, 200, 300), one from a muted sender, one I sent, one note to self.
const dmPeers = new Map([
  ['alice', [{ t: 100 }, { t: 200 }, { t: 300 }, { t: 310, mine: true }]],
  ['muted', [{ t: 150 }]],
  ['me', [{ t: 250, mine: true }]],
]);
const marks = { alice: 0, muted: 0, me: 0 };
const dmSeenFor = pk => marks[pk] || 0, isMutedAuthor = pk => pk === 'muted';
let desktop = false;
const window = { PCOS: { isOn: () => desktop } };
const PCOS = window.PCOS;
let list = [];
const notifList = () => list, _notifTs = e => e.created_at, _aheadRead = () => new Set(),
      _arrivedUnread = () => false, _concordMentions = () => [], _tgUnread = () => [], _smsUnread = () => [];
eval(dmFn + '\nwindow.PCdmUnreadSince = dmUnreadSince;\n' + bellFn + '\nglobalThis.__bell = notifUnread;');

// The web/phone layout: DMs badge Messages only.
if (__bell() !== 0) fail('outside the desktop, unread DMs lit the bell with ' + __bell());
desktop = true;
// alice x3 + the note to self (a note to self is how the server talks to its admin) = 4; muted and mine are not.
if (__bell() !== 4) fail('unread DMs should light the bell with 4, got ' + __bell());
list = [{ id: 'm1', kind: 1, created_at: 1000 }];                // plus a mention
if (__bell() !== 5) fail('a mention and 4 DMs should be 5, got ' + __bell());
marks.alice = 200;                                               // read alice's first two (on any device)
if (__bell() !== 3) fail('read DMs still lit the bell: ' + __bell());
seenNotif.last = 2000;                                           // Notifications opened: the bell empties
if (__bell() !== 0) fail('opening Notifications left the bell at ' + __bell());
dmPeers.get('alice').push({ t: 2100 });                          // a NEW DM afterwards lights it again
if (__bell() !== 1) fail('a DM after Notifications was opened did not light the bell: ' + __bell());
desktop = false;                                                 // leaving the desktop: back to notifications only
if (__bell() !== 0) fail('outside the desktop the DM still lit the bell: ' + __bell());
if (S._dmUnread !== 3) fail('the bell changed the Messages count');
console.log('bell dms runtime ok');
