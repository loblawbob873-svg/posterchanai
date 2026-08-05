/* Two browsers, one relay — the situation every bookmark-sync bug has actually been in.
 *
 * Every earlier test drove ONE engine. The failures were all in the interaction between two: a
 * bookmark on Chrome's toolbar lives in Firefox's Bookmarks Menu, a delete on one has to reach the
 * other, and merging repeatedly must not grow anything. None of that is visible with a single engine
 * and a hand-written list of "remote" items, which is why it kept reaching a real browser instead.
 *
 * The module keeps its state in module scope, so two engines cannot coexist in one context. Each
 * browser therefore gets its OWN vm context with its own copy of bookmarks.js — genuinely
 * independent maps, items and listeners — and they exchange events through a shared relay object,
 * exactly as they do through a real one (newest created_at wins, empty content is a tombstone).
 *
 * Usage: node two_browser_sim.js   → prints one JSON line per scenario, non-zero exit on failure.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'extension', 'bookmarks.js'), 'utf8');
const tick = () => new Promise(r => setTimeout(r, 0));

/* ONE shared logical clock, on the real Unix-seconds scale, read by BOTH the engine (through a Date
 * injected into its context) AND the relay when it stamps created_at.
 *
 * This is not a detail — it is the difference between a faithful sim and one that lies. In a real
 * browser every `_at` the engine records and every `created_at` the relay stores is
 * Math.floor(Date.now()/1000): one monotonic scale, so a tombstone published LATER genuinely outranks
 * the create it removes. The first version of this file gave the relay its own counter starting at
 * 1000 while the engine self-stamped items._at from the real Date.now() (~1.7e9) — so a browser that
 * had PUBLISHED a bookmark would compare its ~1.7e9 `_at` against a ~1050 tombstone and reject it as
 * "older", every time. That masked a real bug (a folder delete not propagating) as a pass and would
 * have let any tombstone-vs-publisher regression through. A single shared clock is the fix, and every
 * write advancing it by a second models "each write is a distinct instant" exactly as reality does. */
const RealDate = Date;
const CLOCK = { t: 1700000000 };
function FakeDate(x) { return arguments.length ? new RealDate(x) : new RealDate(CLOCK.t * 1000); }
FakeDate.now = () => CLOCK.t * 1000;

/* A shared relay: syncId -> the latest event. Publishing replaces; every browser reads them all. */
function makeRelay() {
  const events = new Map();
  return {
    events,
    publishes: 0,
    publish(syncId, item) {
      CLOCK.t += 1; this.publishes += 1;
      events.set(syncId, { created_at: CLOCK.t, content: item === null ? '' : JSON.stringify(item) });
      return true;
    },
    /* Plant a raw event under an ARBITRARY id — the way an older buggy version left a bookmark on the
       relay under a random id. Several of these per URL is the mess a real profile is actually in. */
    seed(id, item, at) { events.set(id, { created_at: at, content: JSON.stringify(item) }); },
    liveCount() { return [...events.values()].filter(e => e.content).length; },
    all() { return [...events.entries()]; },
  };
}

/* One browser: its own bookmark tree, its own storage, its own copy of the engine. */
function makeBrowser(name, relay, rootDefs) {
  const nodes = { r: { id: 'r', children: [] } };
  for (const [id, title] of rootDefs) {
    nodes[id] = { id, title, parentId: 'r', children: [] };
    nodes.r.children.push(id);
  }
  let seq = 0;
  const mk = (parentId, title, url) => {
    const n = { id: `${name}${++seq}`, title, url, parentId, children: [] };
    nodes[n.id] = n; nodes[parentId].children.push(n.id); return n;
  };
  const hydrate = (id) => Object.assign({}, nodes[id],
    { children: (nodes[id].children || []).map(hydrate) });

  const store = {};
  const listeners = { onCreated: [], onChanged: [], onMoved: [], onRemoved: [] };
  const reg = (k) => ({ addListener: (fn) => listeners[k].push(fn) });
  let fired = 0;
  const calls = { getTree: 0, getChildren: 0, get: 0 };
  const fire = (k, id) => { fired++; for (const fn of listeners[k]) { try { fn(id, {}); } catch (_) {} } };
  const B = {
    storage: { local: {
      get: async (keys) => { const out = {}; for (const k of [].concat(keys)) if (k in store) out[k] = store[k]; return out; },
      set: async (o) => { Object.assign(store, o); },
    } },
    bookmarks: {
      onCreated: reg('onCreated'), onChanged: reg('onChanged'),
      onMoved: reg('onMoved'), onRemoved: reg('onRemoved'),
      getTree: async () => { calls.getTree++; await tick(); return [hydrate('r')]; },
      getChildren: async (id) => { calls.getChildren++; await tick(); return (nodes[id].children || []).map(c => nodes[c]); },
      get: async (id) => { calls.get++; await tick(); return nodes[id] ? [nodes[id]] : []; },
      /* A REAL BROWSER FIRES ITS LISTENERS for the extension's own writes too, and it fires them as
         part of the call resolving — before any code after `await create(...)` runs. That ordering is
         the whole point: an engine that registers "I am writing this id" AFTERWARDS has already lost
         the race, and its own creation comes back as a user edit to be republished. */
      create: async (o) => { await tick(); const n = mk(o.parentId, o.title, o.url); fire('onCreated', n.id); return n; },
      update: async (id, o) => { await tick(); Object.assign(nodes[id], o); fire('onChanged', id); },
      move: async (id, o) => {
        await tick(); const n = nodes[id];
        nodes[n.parentId].children = nodes[n.parentId].children.filter(c => c !== id);
        n.parentId = o.parentId; nodes[o.parentId].children.push(id);
        fire('onMoved', id);
      },
      remove: async (id) => {
        await tick(); const n = nodes[id];
        if (!n) return;
        nodes[n.parentId].children = nodes[n.parentId].children.filter(c => c !== id);
        delete nodes[id];
        fire('onRemoved', id);
      },
      /* Deleting a FOLDER, the way a real browser does it: the whole subtree disappears and ONE
         onRemoved fires — for the folder, NOT once per descendant. An engine that only tombstones the
         ids it is handed therefore never hears about the bookmarks inside a deleted folder, which is
         its own class of bug and invisible to a mock that fires per leaf. */
      removeTree: async (id) => {
        await tick(); const n = nodes[id];
        if (!n) return;
        const kill = (x) => { for (const c of (nodes[x] && nodes[x].children || []).slice()) kill(c); delete nodes[x]; };
        nodes[n.parentId].children = nodes[n.parentId].children.filter(c => c !== id);
        kill(id);
        fire('onRemoved', id);
      },
    },
  };

  // Its own module instance, in its own context. Date is the SHARED fake clock (see CLOCK) so the
  // engine's self-stamped `_at` and the relay's `created_at` sit on one monotonic scale.
  const ctx = vm.createContext({ crypto: require('crypto').webcrypto, setTimeout, clearTimeout,
                                 console, TextEncoder, TextDecoder, Date: FakeDate });   // present in a worker and an event page
  ctx.self = ctx;
  vm.runInContext(SRC, ctx);
  const engine = ctx.PCBookmarks.engine;

  /* What a backup restore or an HTML import looks like from in here: nodes appearing one at a time,
     each with its own onCreated — NOT one bulk notification. */
  const restore = (parentId, n, prefix) => {
    for (let i = 0; i < n; i++) { const x = mk(parentId, `${prefix}${i}`, `https://${prefix}${i}.example/`); fire('onCreated', x.id); }
  };

  return {
    name, nodes, mk, restore, engine, store, events: () => fired, calls,
    async init() {
      await engine.init({
        B,
        open: async (ct) => JSON.parse(ct),
        publish: async (syncId, item) => relay.publish(syncId, item),
        isFull: () => true,
        why: () => '',
      });
      await engine.setEnabled(true);
    },
    /* Read everything off the relay, the way the subscription does. */
    async pull() { for (const [id, ev] of relay.all()) await engine.absorb(id, ev); },
    /* Fire every absorb WITHOUT awaiting between them — exactly what the socket's onmessage does. The
       engine must serialise internally, or concurrent absorbs of one URL each create before either maps
       and it duplicates (and floods the bookmark backend, which is the lock-up). */
    async pullConcurrent() { await Promise.all(relay.all().map(([id, ev]) => engine.absorb(id, ev))); },
    async merge(opts) { return engine.union(opts); },
    urls() { return Object.values(nodes).filter(n => n.url).map(n => n.url).sort(); },
    folders(title) { return Object.values(nodes).filter(n => !n.url && n.title === title).length; },
    nodeByTitle(title) { return Object.values(nodes).find(n => n.title === title); },
    nodeByUrl(url) { return Object.values(nodes).find(n => n.url === url); },
    /* Delete straight out of the tree, i.e. what a user does when nothing is listening. */
    rip(url) {
      const n = Object.values(nodes).find(x => x.url === url);
      if (!n) return;
      nodes[n.parentId].children = nodes[n.parentId].children.filter(c => c !== n.id);
      delete nodes[n.id];
    },
    /* Delete a whole FOLDER the way the UI does — one onRemoved for the subtree (see removeTree). */
    ripFolder(title) { const n = this.nodeByTitle(title); if (n) return B.bookmarks.removeTree(n.id); },
    /* Delete a single bookmark the way the UI does — fires onRemoved (unlike rip, which is silent). */
    ripLive(url) { const n = this.nodeByUrl(url); if (n) return B.bookmarks.remove(n.id); },
    /* Drag a bookmark into another folder, firing onMoved the way the browser does. */
    moveTo(url, folderTitle) {
      const n = this.nodeByUrl(url), p = this.nodeByTitle(folderTitle);
      if (n && p) return B.bookmarks.move(n.id, { parentId: p.id });
    },
    /* Edit a bookmark's URL in place, firing onChanged the way the browser does. */
    editUrl(oldUrl, newUrl) {
      const n = this.nodeByUrl(oldUrl);
      if (n) return B.bookmarks.update(n.id, { url: newUrl });
    },
  };
}

const CHROME_ROOTS = [['1', 'Bookmarks bar'], ['2', 'Other bookmarks']];
const FIREFOX_ROOTS = [['toolbar_____', 'Bookmarks Toolbar'], ['menu________', 'Bookmarks Menu'],
                       ['unfiled_____', 'Other Bookmarks']];

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail });
  if (!ok) process.exitCode = 1;
}

/* A full round: both browsers read everything, then merge, twice each, so that anything one of them
 * publishes is seen by the other and the pair reaches a fixed point. */
async function settle(a, b, opts) {
  for (let i = 0; i < 2; i++) {
    await a.pull(); await a.merge(opts);
    await b.pull(); await b.merge(opts);
    await drain();
  }
}

/* Local edits are published on a debounce, so a round is not over when the calls return. Without
   this wait every check on what a local change did would pass by simply never having run. */
const drain = () => new Promise(r => setTimeout(r, 600));

(async () => {
  // ---- 1. The same URL, filed differently on each browser --------------------------------------
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    a.mk('1', 'News', 'https://news.example/');           // Chrome: on the toolbar
    b.mk('menu________', 'News', 'https://news.example/'); // Firefox: in the menu
    await a.init(); await b.init();
    await settle(a, b);
    check('same url filed differently stays one bookmark each',
      a.urls().length === 1 && b.urls().length === 1, { chrome: a.urls(), firefox: b.urls() });
  }

  // ---- 2. Adding on one appears on the other ----------------------------------------------------
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    a.mk('1', 'Only here', 'https://new.example/');
    await a.init(); await b.init();
    await settle(a, b);
    check('an add propagates', b.urls().includes('https://new.example/'), { firefox: b.urls() });
  }

  // ---- 2b. A SETTLED PAIR MUST GO QUIET ---------------------------------------------------------
  // Not "the counts stop growing" — the counts can be stable while the two browsers republish the
  // same bookmarks at each other forever, which is a write storm: thousands of bookmark writes and
  // relay publishes, and a browser that stops responding. Nothing has ever asserted this.
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    for (let i = 0; i < 5; i++) a.mk('1', 'A' + i, `https://a${i}.example/`);
    for (let i = 0; i < 5; i++) b.mk('menu________', 'B' + i, `https://b${i}.example/`);
    await a.init(); await b.init();
    await settle(a, b);
    const before = relay.publishes;
    await settle(a, b);
    await settle(a, b);
    check('a settled pair publishes nothing further',
      relay.publishes === before, { afterSettle: before, afterTwoMore: relay.publishes });
  }

  // ---- 3. Merging repeatedly changes nothing (the duplication report) ---------------------------
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    a.mk('1', 'A', 'https://a.example/');
    a.mk('1', 'B', 'https://b.example/');
    b.mk('menu________', 'C', 'https://c.example/');
    await a.init(); await b.init();
    await settle(a, b);
    const first = [a.urls().length, b.urls().length];
    for (let i = 0; i < 4; i++) await settle(a, b);       // eight more merges
    check('merging repeatedly is idempotent',
      a.urls().length === first[0] && b.urls().length === first[1] && first[0] === 3,
      { after1: first, after9: [a.urls().length, b.urls().length] });
  }

  // ---- 4. A folder full of bookmarks makes ONE folder ------------------------------------------
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    const f = a.mk('1', 'Work');
    for (let i = 0; i < 12; i++) a.mk(f.id, 'W' + i, `https://w${i}.example/`);
    await a.init(); await b.init();
    await settle(a, b);
    check('a folder arrives once, not once per bookmark',
      b.folders('Work') === 1 && b.urls().length === 12, { folders: b.folders('Work'), urls: b.urls().length });
  }

  // ---- 5. Deleting on one removes it on the other ----------------------------------------------
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    a.mk('1', 'Keep', 'https://keep.example/');
    a.mk('1', 'Gone', 'https://gone.example/');
    await a.init(); await b.init();
    await settle(a, b);
    a.rip('https://gone.example/');                       // deleted with nothing listening
    await settle(a, b);
    check('a delete propagates instead of coming back',
      !a.urls().includes('https://gone.example/') && !b.urls().includes('https://gone.example/') &&
      b.urls().includes('https://keep.example/'), { chrome: a.urls(), firefox: b.urls() });
  }

  // ---- 6. Wholesale loss is not obeyed without confirmation ------------------------------------
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    for (let i = 0; i < 10; i++) a.mk('1', 'X' + i, `https://x${i}.example/`);
    await a.init(); await b.init();
    await settle(a, b);
    for (const u of a.urls().slice()) a.rip(u);           // the whole tree disappears here
    await a.pull(); const stopped = await a.merge();
    await b.pull(); await b.merge();
    check('a wholesale disappearance asks before deleting everywhere',
      stopped.pendingRemovals === 10 && b.urls().length === 10,
      { pending: stopped.pendingRemovals, firefox: b.urls().length });
    await a.merge({ confirmRemovals: true });             // the user says they meant it
    await settle(a, b, { confirmRemovals: true });
    check('…and obeys once confirmed', b.urls().length === 0, { firefox: b.urls().length });
  }

  /* THE LOCK-UP, as a number. A browser fires its listeners for the extension's OWN writes, so an
   * engine that republishes them feeds itself: every apply causes a publish, every publish causes an
   * apply on the other browser, and the pair saturates the relay and the bookmark database until the
   * browser stops responding. Counting writes makes that a failed assertion instead of a force-quit.
   *
   * Ten bookmarks between two browsers is a handful of writes each; hundreds means it is looping. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    for (let i = 0; i < 5; i++) a.mk('1', 'S' + i, `https://s${i}.example/`);
    for (let i = 0; i < 5; i++) b.mk('menu________', 'T' + i, `https://t${i}.example/`);
    await a.init(); await b.init();
    for (let i = 0; i < 5; i++) await settle(a, b);
    const writes = a.events() + b.events();
    /* Ten bookmarks means ten publishes and ten writes: each one is created once on the far side and
       published once. Twenty publishes is the engine republishing its own writes — measured at
       exactly that before the fix — and it compounds with tree size and with every subscription
       round, so the limit is just above correct rather than merely "not catastrophic". */
    check('no write storm', writes <= 20 && relay.publishes <= 12,
      { browserWrites: writes, relayPublishes: relay.publishes });
  }

  /* A REALISTIC TREE. Everything above uses ten bookmarks, where an O(tree) call per arriving
   * bookmark costs nothing and is invisible. A real profile has hundreds, and getTree() serialises
   * the WHOLE tree across the extension boundary every time — so "one full-tree read per bookmark"
   * is quadratic work that presents as the browser locking up while it syncs. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    const folder = a.mk('1', 'Big');
    for (let i = 0; i < 300; i++) a.mk(folder.id, 'N' + i, `https://n${i}.example/`);
    await a.init(); await b.init();
    await settle(a, b);
    check('a large tree does not read the whole tree per bookmark',
      b.calls.getTree <= 20 && a.calls.getTree <= 20,
      { chromeGetTree: a.calls.getTree, firefoxGetTree: b.calls.getTree,
        bookmarks: b.urls().length });
  }

  /* RESTORING A BACKUP, which is what the user actually did. The tree read above happens on the
   * receiving side; this one is driven by the user's OWN bulk action, one event per bookmark, and it
   * needs the path of each — so it used to serialise the entire tree 300 times, on the UI thread,
   * with a blocking publish between each. Same symptom, worse trigger. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    await a.init(); await b.init();
    const before = b.calls.getTree;
    b.restore('toolbar_____', 300, 'r');
    await drain();
    await settle(a, b);
    check('restoring a backup does not read the whole tree per bookmark',
      b.calls.getTree - before <= 20,
      { treeReadsForRestore: b.calls.getTree - before, restored: 300,
        reachedChrome: a.urls().length });
  }

  /* SEVERAL MERGES AT ONCE, which is the normal case rather than an edge one: EOSE fires per relay,
   * again on every reconnect, and again from the periodic connect check. Overlapping merges each read
   * the whole tree and each plan against a map the others are still mutating. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    for (let i = 0; i < 20; i++) a.mk('1', 'M' + i, `https://m${i}.example/`);
    await a.init(); await b.init();
    const before = a.calls.getTree;
    await Promise.all([a.merge(), a.merge(), a.merge(), a.merge(), a.merge()]);
    await b.pull(); await b.merge(); await drain();
    check('overlapping merges do not pile up or duplicate',
      a.calls.getTree - before <= 5 && b.urls().length === 20 && relay.publishes <= 20,
      { treeReads: a.calls.getTree - before, arrived: b.urls().length, publishes: relay.publishes });
  }

  /* THE OFF SWITCH IS REAL. Not "syncs less" — the engine must not call the bookmark API at all, so a
   * user who does not want this cannot be slowed down by it. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS);
    for (let i = 0; i < 50; i++) a.mk('1', 'Q' + i, `https://q${i}.example/`);
    await a.init();
    await a.engine.setEnabled(false);
    /* Baseline AFTER init: init enables sync and merges, so it legitimately publishes the 50 that
       already existed. Measuring from zero here reports that as the off switch leaking. */
    const before = { t: a.calls.getTree, c: a.calls.getChildren, p: relay.publishes };
    a.restore('1', 50, 'z');                     // a burst of user edits while sync is off
    await drain();
    await a.pull();
    check('with sync off the engine never touches the bookmark api',
      a.calls.getTree === before.t && a.calls.getChildren === before.c &&
      relay.publishes === before.p,
      { treeReads: a.calls.getTree - before.t, childReads: a.calls.getChildren - before.c,
        publishes: relay.publishes - before.p });
  }

  /* A RECONNECT re-delivers everything the relay holds, with the timestamps it already had. Absorbing
   * those again means decrypting each one and re-applying a bookmark that did not change — the whole
   * library, per relay, every time the socket comes back. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    for (let i = 0; i < 40; i++) a.mk('1', 'S' + i, `https://s${i}.example/`);
    await a.init(); await b.init();
    await settle(a, b);
    const before = { c: b.calls.getChildren, t: b.calls.getTree, p: relay.publishes };
    for (let round = 0; round < 3; round++) await b.pull();      // three reconnects, same events
    await drain();
    check('reconnecting does not re-apply the whole library',
      b.calls.getChildren - before.c === 0 && b.calls.getTree - before.t === 0 &&
      relay.publishes === before.p && b.urls().length === 40,
      { childReads: b.calls.getChildren - before.c, treeReads: b.calls.getTree - before.t,
        publishes: relay.publishes - before.p, bookmarks: b.urls().length });
  }

  /* DELETING A FOLDER MUST SYNC — the bug a real user hit and the sim could not see. A browser fires
   * ONE onRemoved for a deleted folder, never one per bookmark inside it, so an engine that only
   * tombstones the ids it is handed leaves every bookmark in that folder alive on the relay and on the
   * other browser. It "worked" only if the user then pressed Merge, which is not what deleting means.
   * This is LIVE: the folder is deleted on Firefox and Chrome only pulls the subscription — no merge. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    const w = a.mk('1', 'Work');
    for (let i = 0; i < 6; i++) a.mk(w.id, 'W' + i, `https://w${i}.example/`);
    a.mk('1', 'Loose', 'https://loose.example/');
    await a.init(); await b.init();
    await settle(a, b);
    await b.ripFolder('Work');                          // one onRemoved, for the folder
    await drain();                                       // the removal sweep is debounced
    await a.pull(); await drain();                       // Chrome just receives — no merge
    check('deleting a folder syncs the bookmarks inside it, live',
      a.urls().length === 1 && a.urls()[0] === 'https://loose.example/' &&
      b.urls().length === 1,
      { chrome: a.urls(), firefox: b.urls() });
  }

  /* …and the sweep tombstones the CHILDREN, not merely forgets them: a second browser that was offline
   * during the delete still learns of it from the relay when it comes back. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    const w = a.mk('1', 'Trip');
    for (let i = 0; i < 4; i++) a.mk(w.id, 'T' + i, `https://t${i}.example/`);
    await a.init(); await b.init();
    await settle(a, b);
    await a.ripFolder('Trip'); await drain();            // deleted on Chrome while Firefox is idle
    const tombstones = [...relay.events.values()].filter(e => !e.content).length;
    await settle(a, b);
    check('a folder delete leaves tombstones, so an offline device also drops them',
      tombstones === 4 && b.urls().length === 0 && a.urls().length === 0,
      { tombstones, chrome: a.urls().length, firefox: b.urls().length });
  }

  /* A MOVE between folders on one browser is a location edit, not a new bookmark. It must not
   * duplicate, and it must land in the new folder on the other browser. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    a.mk('1', 'Alpha'); a.mk('1', 'Beta');
    a.mk(a.nodeByTitle('Alpha').id, 'M', 'https://m.example/');
    await a.init(); await b.init();
    await settle(a, b);
    await a.moveTo('https://m.example/', 'Beta'); await drain();
    await settle(a, b);
    const bm = b.nodeByUrl('https://m.example/');
    const parent = bm && b.nodes[bm.parentId];
    check('a move does not duplicate and lands in the new folder',
      a.urls().filter(u => u === 'https://m.example/').length === 1 &&
      b.urls().filter(u => u === 'https://m.example/').length === 1 &&
      parent && parent.title === 'Beta',
      { chrome: a.urls(), firefox: b.urls(), firefoxParent: parent && parent.title });
  }

  /* EDITING A URL is an add plus a delete (the sync id is derived from the url). The old one must go
   * and the new one must arrive — no orphan left behind on either browser. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS), b = makeBrowser('f', relay, FIREFOX_ROOTS);
    a.mk('1', 'E', 'https://old.example/');
    await a.init(); await b.init();
    await settle(a, b);
    await a.editUrl('https://old.example/', 'https://new.example/');   // in-place URL edit → onChanged
    await drain(); await settle(a, b);
    check('editing a url syncs the new and drops the old',
      b.urls().includes('https://new.example/') && !b.urls().includes('https://old.example/') &&
      a.urls().length === 1 && b.urls().length === 1,
      { chrome: a.urls(), firefox: b.urls() });
  }

  /* THE REVENANT: "Brave brings back everything I delete." A relay polluted by older versions holds
   * SEVERAL live events per URL (each published under a random id). Three separate bugs conspired:
   *   - absorbing each duplicate event created ANOTHER copy of the bookmark (the duplicates you see,
   *     and the pile of create() calls that locks the browser up);
   *   - deleting the visible copy tombstoned only the ONE id it was mapped to, so the next stale event
   *     recreated it;
   *   - and a merge that mapped the winning id onto the bookmark and forgot the losing id wiped the
   *     bookmark's reverse mapping (forget cleared rmap[b] even though b now belonged to the winner),
   *     so a later delete found no id at all to tombstone.
   * A delete must now kill EVERY event for the URL and actually stay dead. */
  {
    const relay = makeRelay();
    const brave = makeBrowser('c', relay, CHROME_ROOTS);
    const URLS = ['https://a.example/', 'https://b.example/', 'https://c.example/'];
    URLS.forEach((u, i) => {
      relay.seed('oldrandom_' + i, { title: 'Old ' + i, url: u, folder: '', root: 'toolbar' }, 1000 + i);
      if (i === 0) relay.seed('another_' + i, { title: 'Dup ' + i, url: u, folder: '', root: 'toolbar' }, 1005 + i);
    });
    await brave.init();
    for (let r = 0; r < 2; r++) { await brave.pull(); await brave.merge(); await drain(); }
    const dupA = brave.urls().filter(u => u === 'https://a.example/').length;
    for (const u of brave.urls().slice()) await brave.ripLive(u);   // live deletes, one onRemoved each
    await drain();
    for (let r = 0; r < 3; r++) { await brave.pull(); await brave.merge(); await drain(); }
    check('deleting stays deleted even with stale duplicate events on the relay',
      dupA === 1 && brave.urls().length === 0 && relay.liveCount() === 0,
      { duplicateCopiesOfA: dupA, afterDelete: brave.urls(), liveEventsLeft: relay.liveCount() });
  }

  /* "108 bookmarks are missing… it comes back no matter how many times I click Merge." Two browsers,
   * both on the fixed build, a relay polluted with duplicate events. The deletions are detected on
   * MERGE (the MV3 worker was asleep when they were deleted, so no live tombstone fired) and confirmed
   * with "Delete N everywhere" — but the confirm path (the union's removal loop) tombstoned only the
   * ONE mapped id per URL, not the duplicate siblings. The surviving sibling resurrected the bookmark
   * on the next sync, the other browser kept it alive, and the "N missing" prompt returned forever.
   * Confirming a bulk delete must kill EVERY event for each URL, the same rule live-delete already uses. */
  {
    const relay = makeRelay();
    const brave = makeBrowser('c', relay, CHROME_ROOTS), fox = makeBrowser('f', relay, FIREFOX_ROOTS);
    const URLS = [];
    for (let i = 0; i < 6; i++) { const u = `https://x${i}.example/`; URLS.push(u);
      relay.seed('r1_' + i, { title: 'X' + i, url: u, folder: '', root: 'toolbar' }, 1000 + i * 4);
      if (i % 2 === 0) relay.seed('r2_' + i, { title: 'X' + i, url: u, folder: '', root: 'toolbar' }, 1002 + i * 4); }
    await brave.init(); await fox.init();
    await settle(brave, fox);
    const synced = [brave.urls().length, fox.urls().length];
    // Delete every bookmark on Brave with NO onRemoved (models the MV3 worker asleep at delete time),
    // so it shows up as "missing on merge" rather than a live tombstone.
    for (const u of brave.urls().slice()) brave.rip(u);
    // Confirm the bulk delete repeatedly while Firefox — which still holds them — keeps syncing.
    for (let round = 0; round < 4; round++) {
      await brave.pull(); await brave.merge({ confirmRemovals: true });
      await fox.pull(); await fox.merge();
      await drain();
    }
    check('confirming a bulk delete stays deleted with duplicate events and a second browser',
      synced[0] === 6 && synced[1] === 6 &&
      brave.urls().length === 0 && fox.urls().length === 0 && relay.liveCount() === 0,
      { synced, braveAfter: brave.urls().length, foxAfter: fox.urls().length, relayLive: relay.liveCount() });
  }

  /* THE LOCK-UP AT REAL SCALE — "sync makes Brave quit." A relay left polluted by older versions holds
   * SEVERAL events per URL. The dedupe that keeps it to one bookmark did an O(items) scan AND a
   * bookmarks.get() for every duplicate event — O(n²) plus a get()-storm: ~1800 get() calls and tens of
   * seconds on the UI thread for a real library, i.e. the browser freezing while it syncs. A URL index
   * makes it O(1) with NO per-event get(). Absorbing a heavily-duplicated relay must stay cheap: no
   * get()-storm, a handful of tree reads, and exactly one bookmark per URL. */
  {
    const relay = makeRelay();
    const a = makeBrowser('c', relay, CHROME_ROOTS);
    const N = 300, DUP = 3;
    for (let i = 0; i < N; i++) { const u = `https://s${i}.example/p`;
      for (let d = 0; d < DUP; d++) relay.seed(`rnd_${i}_${d}`, { title: 'B' + i, url: u, folder: 'F' + (i % 15), root: 'toolbar' }, 1000 + i * 10 + d); }
    await a.init();
    const before = { get: a.calls.get, tree: a.calls.getTree };
    await a.pull();                              // absorb the whole polluted relay
    await drain();
    check('a polluted relay syncs without a get()-storm or quadratic work',
      a.calls.get - before.get <= 5 && a.calls.getTree - before.tree <= 5 && a.urls().length === N,
      { events: relay.liveCount(), getCalls: a.calls.get - before.get,
        treeReads: a.calls.getTree - before.tree, bookmarks: a.urls().length });
  }

  console.log(JSON.stringify(results, null, 1));
})();
