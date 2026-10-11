/* A DM BADGES MESSAGES, NEVER THE BELL (operator decision, 2026-10-11).
 *
 * Unread DMs used to be summed into the notification bell ("i see no notification bell when DM's come
 * in"), which made one message light two badges and left the bell lit for something Notifications only
 * pointed away from. Now the bell is notifications (+ the update row + Concord mentions) and Messages
 * keeps its own count. Runs the SHIPPED notifs.js notifUnread with stand-ins for its state -- including
 * a `window.PCdmUnreadSince` that still answers 13, so a bell that asks it at all fails here. */
import fs from 'fs';
const src = f => fs.readFileSync(new URL('../../static/js/client/' + f, import.meta.url), 'utf8');
const slice = (text, start) => { const i = text.indexOf(start); if (i < 0) throw new Error('missing ' + start);
  let d = 0, j = text.indexOf('{', i); for (let k = j; k < text.length; k++) { d += text[k] === '{'; d -= text[k] === '}'; if (!d) return text.slice(i, k + 1); } };
const bellFn = slice(src('notifs.js'), 'function notifUnread(');
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

const S = { ME: { pubkey: 'me' }, _dmUnread: 13, _newBuild: false, _apkUpdate: false, _updBadge: false, _notifEpoch: 0 };
const seenNotif = { last: 0 };
const window = { PCdmUnreadSince: () => 13 };
let list = [];
const notifList = () => list, _notifTs = e => e.created_at, _aheadRead = () => new Set(),
      _arrivedUnread = () => false, _concordMentions = () => [];
eval(bellFn + '\nglobalThis.__bell = notifUnread;');

if (__bell() !== 0) fail('13 unread DMs lit the bell with ' + __bell() + ' -- DMs badge Messages only');
list = [{ id: 'm1', kind: 1, created_at: 1000 }];               // a reply/mention still rings it
if (__bell() !== 1) fail('a mention should light the bell with 1, got ' + __bell());
seenNotif.last = 2000;                                          // Notifications opened
if (__bell() !== 0) fail('opening Notifications left the bell at ' + __bell());
if (S._dmUnread !== 13) fail('the bell changed the Messages count');
console.log('bell dms runtime ok');
