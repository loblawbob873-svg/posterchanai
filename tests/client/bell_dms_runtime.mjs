/* Opening Notifications empties the bell -- unread DMs included -- while Messages keeps counting them.
 * "still says 13" / "i opened messages and it cleared": the 13 were DMs, which only Messages marked read.
 * Runs the SHIPPED dms.js dmUnreadSince and notifs.js notifUnread with stand-ins for their state. */
import fs from 'fs';
const src = f => fs.readFileSync(new URL('../../static/js/client/' + f, import.meta.url), 'utf8');
const slice = (text, start) => { const i = text.indexOf(start); if (i < 0) throw new Error('missing ' + start);
  let d = 0, j = text.indexOf('{', i); for (let k = j; k < text.length; k++) { d += text[k] === '{'; d -= text[k] === '}'; if (!d) return text.slice(i, k + 1); } };
const dmFn = slice(src('dms.js'), 'function dmUnreadSince(');
const bellFn = slice(src('notifs.js'), 'function notifUnread(');
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

const ME = 'me', settings = { dmSeen: 0 };
const dmPeers = new Map([['alice', Array.from({ length: 13 }, (_, i) => ({ t: 1000 + i, mine: false }))]]);
const S = { ME: { pubkey: ME }, _dmUnread: 13, _newBuild: false, _apkUpdate: false, _updBadge: false, _notifEpoch: 0 };
const seenNotif = { last: 0 };
const window = {};
const ClientSettings = { get: (k, d) => (k in settings ? settings[k] : d) };
const isMutedAuthor = () => false, notifList = () => [], _notifTs = e => e.created_at, _aheadRead = () => new Set(),
      _concordMentions = () => [];
eval(dmFn + '\nwindow.PCdmUnreadSince = dmUnreadSince;\n' + bellFn + '\nglobalThis.__bell = notifUnread;');

if (__bell() !== 13) fail('13 unread DMs should light the bell with 13, got ' + __bell());
seenNotif.last = 2000;                                   // Notifications opened (markNotifsRead)
if (__bell() !== 0) fail('opening Notifications left the bell at ' + __bell());
if (S._dmUnread !== 13) fail('opening Notifications changed the Messages count');
dmPeers.get('alice').push({ t: 3000, mine: false }); S._dmUnread = 14;
if (__bell() !== 1) fail('a DM after opening Notifications should light the bell with 1, got ' + __bell());
settings.dmSeen = 4000; S._dmUnread = 0;                 // read in Messages
if (__bell() !== 0) fail('reading the DMs left the bell lit: ' + __bell());
console.log('bell dms runtime ok');
