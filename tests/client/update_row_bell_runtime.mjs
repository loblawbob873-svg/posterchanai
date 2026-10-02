/* "on desktop I have a notification bell stuck at 1 despite clicking on it many times".
 *
 * A desktop whose bundled client is older than the server's carries an "App update available" row
 * until the app is updated -- and the bell counted that row for as long as it existed, so opening
 * Notifications (which is what clicking the bell does) could never clear it. Runs the SHIPPED
 * notifs.js (the count every bell reads) and the SHIPPED notifview.js markNotifsRead (what opening
 * Notifications does), wired to one shared state exactly as app.js wires them.
 */
import fs from 'fs';
import vm from 'vm';
const read = f => fs.readFileSync(new URL('../../static/js/client/' + f, import.meta.url), 'utf8');
const store = new Map();
const localStorage = { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
const window = { addEventListener() {}, localStorage };
const ctx = { window, localStorage, console, document: { visibilityState: 'visible', hasFocus: () => true, querySelectorAll: () => [] },
  Store: { all: () => [] }, setTimeout: () => 0, clearTimeout() {} };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(read('notifs.js'), ctx);
vm.runInContext(read('notifview.js'), ctx);
const S = { ME: { pubkey: 'f'.repeat(64) }, VIEW: 'global', _notifEpoch: 0, _dmUnread: 0,
            _newBuild: false, _apkUpdate: false, _desktopUpdate: false, _updBadge: false, _updApplying: false };
const seenNotif = { last: 0 };
const anyFn = new Proxy({}, { get: (_t, k) => (k === 'then' ? undefined : (() => {})) });
const dep = new Proxy({ state: S, seenNotif, $$: () => [], $: () => null, isMutedAuthor: () => false },
                      { get: (t, k) => (k in t ? t[k] : anyFn[k]) });
const N = ctx.window.PCNotifsFactory(dep), V = ctx.window.PCNotifViewFactory(dep);
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

/* _checkDesktopUpdate finds a newer build: the row appears and the bell lights. */
S._desktopUpdate = true; S._apkUpdate = true; S._updBadge = true;
if (N.notifUnread() !== 1) fail('a newly found update did not light the bell: ' + N.notifUnread());
/* Clicking the bell opens Notifications. */
V.markNotifsRead();
if (N.notifUnread() !== 0) fail('the bell still counts the update after Notifications was opened (stuck at ' + N.notifUnread() + ')');
if (!(S._apkUpdate || S._newBuild)) fail('opening Notifications removed the update row itself');
/* A number never appears without its row. */
S._apkUpdate = false; S._desktopUpdate = false; S._updBadge = true;
if (N.notifUnread() !== 0) fail('the bell counted an update with no update row');
/* A NEW build found later lights it again. */
S._newBuild = true; S._updBadge = true;
if (N.notifUnread() !== 1) fail('a later update did not light the bell');
console.log('update row bell runtime ok');
