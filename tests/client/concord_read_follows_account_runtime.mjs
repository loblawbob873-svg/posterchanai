/* A CONCORD MENTION READ ON ONE DEVICE IS READ ON EVERY DEVICE.
 *
 * "old concord notifications appear as new notifications when you load concord on a different device".
 * Read state was localStorage only, and a device's first read of a channel puts the last week's mentions
 * in the bell -- so each new device re-announced what had been read on the others. Drives the SHIPPED
 * notifyMentions / noteChannelRead against a fake relay holding the account's pcai:concord-read doc.
 */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const ME = 'a'.repeat(64), OTHER = 'b'.repeat(64);
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
const tick = ms => new Promise(r => setTimeout(r, ms));

function boot(seed) {
  const store = new Map(Object.entries(seed || {}));
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const body = { classList: { add: noop, remove: noop, contains: () => false } };
  const document = { body, visibilityState: 'visible', querySelector: () => null, querySelectorAll: () => [], createElement: () => ({ dataset: {} }),
    head: { appendChild: noop }, documentElement: { appendChild: noop }, addEventListener: noop };
  const window = { document, addEventListener: noop, PosterCordReader: { inspectControl: () => ({}) } };
  /* Concord's rooms, ledger and cursors are kept PER ACCOUNT, keyed on this signed-in viewer. */
  window.__PC = { viewer: () => ({ pubkey: ME }), isView: () => false, toast: noop, $: () => null, bumpNotif: noop };
  const context = { window, document, console, URL, atob, btoa, crypto: {}, localStorage,
    sessionStorage: { getItem: () => null, setItem: noop }, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: noop,
    TextEncoder, indexedDB: undefined, location: { href: 'https://poster.place/client' }, AbortController };
  context.globalThis = context;
  vm.runInNewContext(src, context);
  return { C: window.PCConcord, store, window };
}

/* One account's relays: whatever was published is what the next device reads. `mode` lets a test make
 * the relays silent (a timeout: no EOSE) or unreachable. */
function relays() {
  const r = { events: [], mode: 'ok', published: [] };
  r.p = {
    viewer: () => ({ pubkey: ME }),
    osNotify: noop, profOf: () => ({}),
    nip44enc: async (_pk, s) => 'enc:' + s,
    nip44dec: async (_pk, s) => s.slice(4),
    signTemplate: async t => ({ ...t, id: 'id' + r.published.length, pubkey: ME }),
    relayPublish: async ev => { r.published.push(ev); r.events = [ev]; return { ok: true }; },
    relayQuery: async () => {
      if (r.mode === 'down') throw new Error('no socket');
      const out = r.mode === 'silent' ? [] : r.events.slice();
      Object.defineProperty(out, 'complete', { value: r.mode !== 'silent' });
      return out;
    },
  };
  return r;
}

const room = { url: '', naddr: 'naddr1lounge', communityId: 'c'.repeat(64), name: 'Lounge Chat', local: false, channels: [{ name: 'general' }] };
const viewer = { pubkey: ME, npub: 'npub1me', profile: { name: 'verita84' } };
const now = Date.now();
const msg = (id, at, tagged) => ({ id, at, pubkey: OTHER, by: 'mozgus', text: tagged ? 'hey @verita84' : 'hello', tags: tagged ? [['p', ME]] : [] });
const history = [msg('m1', now - 3600e3, true), msg('m2', now - 1800e3, true), msg('m3', now - 600e3, false)];

/* 1. THE REPORT. The phone read #general; the laptop opens Concord for the first time. */
{
  const net = relays();
  const phone = boot();
  await phone.C.__testReadDoc(net.p);
  phone.C.__testNoteChannelRead(net.p, room, 'general', now - 600e3);
  await tick(1800);
  if (net.published.length !== 1) fail(`reading a channel should publish the account's read marks once, got ${net.published.length}`);
  const laptop = boot();
  laptop.C.__testNotifyMentions(net.p, room, history, viewer, 'verita84', 'general');
  await laptop.C.__testReadDoc(net.p);
  if (laptop.C.mentionsUnread() !== 0) fail(`mentions read on the phone came back as new on the laptop: ${laptop.C.mentionsUnread()}`);
}

/* 2. The guard against "fixed it by never counting a first read": nothing was read anywhere, so the
 *    mentions made while no device was running still wait in the bell. */
{
  const net = relays();
  const laptop = boot();
  laptop.C.__testNotifyMentions(net.p, room, history, viewer, 'verita84', 'general');
  await laptop.C.__testReadDoc(net.p);
  if (laptop.C.mentionsUnread() !== 2) fail(`unread mentions were dropped: ${laptop.C.mentionsUnread()}`);
}

/* 3. A relay that never answered is not "no read marks": nothing is written over the account's doc. */
for (const mode of ['silent', 'down']) {
  const net = relays();
  net.events = [{ kind: 30078, pubkey: ME, created_at: 1, content: 'enc:' + JSON.stringify({ v: 1, marks: { ['x\ngeneral']: 5 } }), tags: [['d', 'pcai:concord-read']] }];
  net.mode = mode;
  const dev = boot();
  dev.C.__testNoteChannelRead(net.p, room, 'general', now);
  await dev.C.__testReadDoc(net.p);
  await tick(1800);
  if (net.published.length) fail(`${mode}: published read marks without having read the account's copy`);
}

/* 4. Two devices reading DIFFERENT channels keep both marks (each save re-reads and merges). */
{
  const net = relays();
  const a = boot(), b = boot();
  await a.C.__testReadDoc(net.p); await b.C.__testReadDoc(net.p);   // both loaded the empty doc
  a.C.__testNoteChannelRead(net.p, room, 'general', now - 10);
  await tick(1800);
  b.C.__testNoteChannelRead(net.p, room, 'support', now - 20);
  await tick(1800);
  const last = JSON.parse(net.events[0].content.slice(4)).marks;
  if (Object.keys(last).length !== 2) fail('the second device erased the first device\'s read mark: ' + JSON.stringify(last));
}


/* 5. THE UNREAD BADGE FOLLOWS THE ACCOUNT TOO, AND WITHOUT A RELOAD. "we need to make sure DMs, Telegram,
 *    Concord, read status syncs across devices like we do with notifications, it's confusing on multiple
 *    devices": the room dots and the sidebar count read only this device's cursor, and the synced mark was
 *    read once per session. The laptop is open with the room unread; the phone reads it. */
{
  const seed = { ['pc.concord.rooms.v1.' + ME]: JSON.stringify([room]), ['pc.concord.test.' + room.naddr]: JSON.stringify(history) };
  for (const how of ['refresh', 'live']) {
    const net = relays();
    let live = null;
    net.p.relaySubscribe = (filters, h) => { live = { filters, h }; return 'sub1'; };
    net.p.relayClose = () => { live = null; };   // a fake subscribe needs its close (test_relay_subscription_contract)
    const laptop = boot(seed);
    await laptop.C.__testReadDoc(net.p);
    if (laptop.C.unreadRooms() !== 1) fail(`${how}: the room should start unread on the laptop, got ${laptop.C.unreadRooms()}`);
    if (how === 'live') { laptop.C.__testStartReadSync?.(net.p); await tick(50); if (!live) fail('live: no subscription to the read marks'); }
    const phone = boot(seed);
    await phone.C.__testReadDoc(net.p);
    phone.C.__testNoteChannelRead(net.p, room, 'general', Math.max(...history.map(m => m.at)));
    await tick(1800);
    if (!net.published.length) fail(`${how}: the phone did not publish its read mark`);
    if (how === 'refresh') await laptop.C.__testRefreshReadDoc?.(net.p, true);
    else { live.h.onEvent(net.events[0]); await tick(50); }
    if (laptop.C.unreadRooms() !== 0) fail(`${how}: the room the phone read is still unread on the laptop (${laptop.C.unreadRooms()})`);
  }
}

/* 6. Never un-read: a newer message than the phone's mark is still unread on the laptop, and an OLDER copy of
 *    the doc arriving later changes nothing. */
{
  const later = [...history, msg('m4', now + 5000, false)];
  const seed = { ['pc.concord.rooms.v1.' + ME]: JSON.stringify([room]), ['pc.concord.test.' + room.naddr]: JSON.stringify(later) };
  const net = relays();
  const laptop = boot(seed);
  await laptop.C.__testReadDoc(net.p);
  const phone = boot(seed);
  await phone.C.__testReadDoc(net.p);
  phone.C.__testNoteChannelRead(net.p, room, 'general', now - 600e3);
  await tick(1800);
  await laptop.C.__testRefreshReadDoc(net.p, true);
  if (laptop.C.unreadRooms() !== 1) fail('a message newer than the phone\'s mark was marked read');
}

/* 7. AN ANSWERED INVITATION IS ANSWERED ON EVERY DEVICE AND AFTER EVERY LOGIN. "every time I clear my concord
 *    invites and login, I see you have 12 invites" (2026-10-21): declines lived in this device's localStorage only.
 *    The phone declines; a fresh login on the laptop must refuse to park that invitation again. An invitation to a
 *    community already joined is refused too. */
{
  const net = relays();
  const inviteId = 'e'.repeat(64);
  const phone = boot();
  Object.assign(phone.window.__PC, net.p);
  await phone.C.__testReadDoc(net.p);
  phone.C.noteInviteAnswered(inviteId);
  await tick(1800);
  const saved = net.events.length ? JSON.parse(net.events[0].content.slice(4)) : {};
  if (!(saved.invites || []).includes(inviteId)) fail('the decline was not written to the account\'s synced document: ' + JSON.stringify(saved));
  if (!saved.marks) fail('the read marks must survive the invitation write');
  const laptop = boot({ ['pc.concord.rooms.v1.' + ME]: JSON.stringify([{ ...room, cord: { bundle: { community_id: 'd'.repeat(64) } } }]) });
  Object.assign(laptop.window.__PC, net.p);
  await laptop.C.__testReadDoc(net.p);
  const opts = laptop.C.inviteParkOptions();
  if (!opts.answered(inviteId)) fail('a fresh login parks an invitation the account already declined');
  if (opts.answered('f'.repeat(64))) fail('an unanswered invitation was treated as answered');
  if (!opts.skip({ bundle: { community_id: 'd'.repeat(64) } })) fail('an invitation to a community already joined was parked');
  if (opts.skip({ bundle: { community_id: '1'.repeat(64) } })) fail('an invitation to a new community was refused');
}

/* 8. "i see all my new rooms as unread when i did" / "concord read room activity not syncing across devices"
 *    (2026-10-21). A room the phone OPENED (markRead -- the phone's path, which never went through the desktop-only
 *    publish) is read on the laptop, and a room restored on the laptop WITHOUT an invite coordinate (no naddr: the
 *    membership list carried none) reads the synced mark instead of being unread for ever. */
{
  const bare = { communityId: '9'.repeat(64), name: 'New room', local: false, channels: [{ name: 'general' }] };
  const phoneRoom = { ...bare, naddr: 'naddr1newroom', url: 'https://x/invite/naddr1newroom#s' };
  const hist = [msg('n1', now - 120e3, false), msg('n2', now - 60e3, false)];
  const net = relays();
  const phone = boot({ ['pc.concord.rooms.v1.' + ME]: JSON.stringify([phoneRoom]), ['pc.concord.test.' + phoneRoom.naddr]: JSON.stringify(hist) });
  Object.assign(phone.window.__PC, net.p);
  await phone.C.__testReadDoc(net.p);
  phone.C.__testMarkRead(phoneRoom, 'general');
  await tick(1800);
  if (!net.published.length) fail('opening a room on the phone did not publish the account\'s read mark');
  const laptop = boot({ ['pc.concord.rooms.v1.' + ME]: JSON.stringify([bare]), ['pc.concord.test.' + bare.communityId]: JSON.stringify(hist) });
  Object.assign(laptop.window.__PC, net.p);
  await laptop.C.__testReadDoc(net.p);
  const n = laptop.C.unreadRooms();
  if (n !== 0) fail(`a room read on the phone is unread on the laptop (${n}) -- a room with no naddr must still read the synced mark`);
}

/* 9. "Concord starred channels don't sync across devices?" (2026-10-22). A star made on the phone is a star on the
 *    laptop, an unstar on the laptop reaches the phone (the newer toggle wins, the older star does not outvote it),
 *    and a star made before stars synced -- localStorage only -- is carried into the doc, not lost. */
{
  const R = { communityId: '7'.repeat(64), naddr: 'naddr1starroom', name: 'Star room', local: false, channels: [{ name: 'general' }, { name: 'memes' }, { name: 'old' }] };
  const rooms = { ['pc.concord.rooms.v1.' + ME]: JSON.stringify([R]) };
  const net = relays();
  const legacy = 'pc.concord.star.v2.' + ME + '.' + R.communityId + ':old';
  const phone = boot({ ...rooms, [legacy]: '1' });
  Object.assign(phone.window.__PC, net.p);
  await phone.C.__testReadDoc(net.p);
  phone.C.__testStar(R, 'memes', true);
  await tick(1800);
  const laptop = boot(rooms);
  Object.assign(laptop.window.__PC, net.p);
  if (laptop.C.__testStar(R, 'memes')) fail('a star showed before the account doc was read');
  await laptop.C.__testReadDoc(net.p);
  if (!laptop.C.__testStar(R, 'memes')) fail('a channel starred on the phone is not starred on the laptop');
  if (!laptop.C.__testStar(R, 'old')) fail('a star made before stars synced was lost instead of carried into the doc');
  if (laptop.C.__testStar(R, 'general')) fail('a channel nobody starred came back starred');
  if (laptop.store.get('pc.concord.star.v2.' + ME + '.' + R.communityId + ':memes') !== '1') fail('the synced star was not cached for the next cold paint');
  await tick(5);
  laptop.C.__testStar(R, 'memes', false);
  await tick(1800);
  await phone.C.__testRefreshReadDoc(net.p);
  if (phone.C.__testStar(R, 'memes')) fail('an unstar on the laptop did not reach the phone -- its older star outvoted it');
  if (!phone.C.__testStar(R, 'old')) fail('an unrelated star was dropped by another device\'s save');
  const doc = JSON.parse(net.events[0].content.slice(4));
  if (!doc.stars || !Array.isArray(doc.stars[R.communityId + '\nmemes']) || doc.stars[R.communityId + '\nmemes'][0] !== 0)
    fail('the unstar is not kept in the doc as a 0: ' + JSON.stringify(doc.stars));
  if (doc.marks === undefined || !Array.isArray(doc.invites)) fail('saving stars dropped the read marks or invites');
}

console.log('concord read follows the account: ok');
process.exit(0);
