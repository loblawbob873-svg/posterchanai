/* Bookmark sync — the same shape as the vault, one encrypted event per bookmark.
 *
 * `d = pcai:bm:<id>`, kind 30078, body sealed with the SAME vault key (AES-GCM, authenticated), so a
 * relay holds ciphertext and nothing else: it cannot read a URL, cannot forge one, and the worst it
 * can do is withhold or replay — which newest-created_at-wins bounds to "stale", never
 * "attacker-chosen". Exactly the guarantees the passwords already have, for the same reasons.
 *
 * WHY A MAPPING TABLE. A browser bookmark's id is local to that browser — Firefox and Chrome mint
 * their own, and they change on a restore — so it can never be the sync id. Every synced bookmark
 * therefore has OUR id (random, stable, in the event's d-tag) and a per-browser map to whatever the
 * local tree calls it today. Losing that map is not data loss: rebuild() re-pairs by url+path.
 *
 * DELETION IS THE DANGEROUS DIRECTION and is treated as such:
 *   - A remote tombstone removes a local bookmark ONLY if it is one we have a mapping for, i.e. one
 *     this browser previously synced. A bookmark that predates sync, or belongs to a different
 *     profile, is never touched by a message from the network.
 *   - The first sync after enabling is a UNION and deletes nothing, in either direction. Two devices
 *     with different bookmarks converge by gaining each other's, never by one wiping the other.
 *   - `removed` is a tombstone (empty content), never an omission. "I don't have it" and "it was
 *     deleted" are different facts, and only the second may remove anything.
 *
 * Everything here is worker-safe: no DOM, no localStorage — Chrome runs this file inside a service
 * worker, where none of those exist.
 */
(function (root) {
  'use strict';

  var D_BM = 'pcai:bm:';
  var L_BM = 'pcai-bm';

  // ---------------------------------------------------------------- pure helpers (tested directly)

  /* A bookmark's folder as a PATH of names, not a local parent id. Ids differ per browser; names are
   * what the user actually arranged, and what can be recreated on the other side. */
  function pathOf(nodesById, node) {
    var parts = [], cur = node, guard = 0;
    while (cur && cur.parentId && guard++ < 64) {
      cur = nodesById[cur.parentId];
      if (!cur || !cur.parentId) break;          // the true root has no parent
      parts.unshift(cur.title || '');
    }
    /* Drop the outermost entry: it is the browser's own top-level container, and those have
     * DIFFERENT NAMES per browser — "Bookmarks Menu"/"Bookmarks Toolbar"/"Other Bookmarks" in
     * Firefox, "Bookmarks bar"/"Other bookmarks" in Chrome. Keeping it would make every bookmark
     * synced from Firefox land in a literal folder called "Bookmarks Menu" inside Chrome, once per
     * container, forever. The path is therefore relative to whichever root it lives under, and the
     * root itself is a placement decision made on arrival, not part of a bookmark's identity. */
    parts.shift();
    return parts.filter(Boolean).join('/');
  }

  /* What identifies "the same bookmark" across browsers when the mapping is gone: its URL inside its
   * folder. Not the title — a title is renamed far more often than a bookmark is re-filed, and
   * matching on it re-creates duplicates every time someone tidies one up. */
  function matchKey(item) {
    return (item.folder || '') + '\n' + normUrl(item.url);
  }

  function normUrl(u) {
    u = String(u || '').trim();
    // A trailing slash on an origin is not a different bookmark. Everything else is left alone —
    // stripping query strings would merge genuinely different pages.
    try {
      var x = new URL(u);
      if (x.pathname === '/' && !x.search && !x.hash) return x.origin;
      return x.href;
    } catch (_) { return u; }
  }

  function isSyncable(node) {
    if (!node || !node.url) return false;                      // folders are implied by paths
    var u = String(node.url);
    // Only real web pages. javascript: bookmarklets are executable content and place:/about: entries
    // are browser-internal and meaningless on the other side.
    return /^https?:\/\//i.test(u);
  }

  /* Merge a remote item onto a local one. Newest wins per FIELD-SET, not per field: a bookmark is
   * small and atomic, and half of one version plus half of another is a bookmark neither device has. */
  function newer(a, b) {
    if (!a) return b;
    if (!b) return a;
    return (a._at || 0) >= (b._at || 0) ? a : b;
  }

  /* The two-way plan for a first sync (or a rebuild): what to publish, what to create locally, and
   * what — deliberately — to leave alone. Pure, so the dangerous half is testable without a browser.
   *
   * `local`  : [{ id (browser id), title, url, folder }]
   * `remote` : [{ id (sync id), title, url, folder, _at, removed? }]
   * `map`    : { syncId: browserId } from a previous run
   */
  function planUnion(local, remote, map) {
    map = map || {};
    var byBrowserId = {};
    local.forEach(function (l) { byBrowserId[l.id] = l; });
    var claimed = {};                                   // browser ids already accounted for
    Object.keys(map).forEach(function (sid) { claimed[map[sid]] = sid; });

    var localByKey = {};
    local.forEach(function (l) { localByKey[matchKey(l)] = l; });

    var out = { publish: [], create: [], link: [], skipRemoved: 0 };

    remote.forEach(function (r) {
      if (r.removed) { out.skipRemoved++; return; }      // a union never deletes — see the header
      var mapped = map[r.id] && byBrowserId[map[r.id]];
      if (mapped) return;                                // already present and paired
      var hit = localByKey[matchKey(r)];
      if (hit) { out.link.push({ syncId: r.id, browserId: hit.id }); claimed[hit.id] = r.id; return; }
      out.create.push(r);                                // this browser has never seen it
    });

    local.forEach(function (l) {
      if (claimed[l.id]) return;                         // has a sync id already
      out.publish.push(l);                               // ours, not yet on any relay
    });
    return out;
  }

  root.PCBookmarks = {
    D_BM: D_BM, L_BM: L_BM,
    pathOf: pathOf, matchKey: matchKey, normUrl: normUrl,
    isSyncable: isSyncable, newer: newer, planUnion: planUnion,
  };
})(typeof self !== 'undefined' ? self : this);

/* ---------------------------------------------------------------------------------------------
 * The live engine. Loaded in the background context, next to background.js, and given its handles
 * (the browser API, the crypto, the publisher) rather than reaching for them — so everything above
 * stays pure and testable, and this half has exactly one place to look when a sync misbehaves.
 * ------------------------------------------------------------------------------------------- */
(function (root) {
  'use strict';
  var P = root.PCBookmarks;

  var api = null;        // { B, seal, open, publish, isFull, log }
  var map = {};          // syncId -> browserId
  var rmap = {};         // browserId -> syncId
  var items = {};        // syncId -> { title, url, folder, _at, removed }
  var on = false;
  /* Ids this engine is currently writing into the tree. The browser fires onCreated/onChanged for
   * OUR OWN writes too, and republishing those is an echo: two devices then bounce one bookmark back
   * and forth, each edit newer than the last, forever. */
  var writing = new Set();

  function remember(syncId, browserId) {
    map[syncId] = browserId; rmap[browserId] = syncId;
    api.B.storage.local.set({ bmMap: map });
  }
  function forget(syncId) {
    var b = map[syncId];
    delete map[syncId]; if (b) delete rmap[b];
    api.B.storage.local.set({ bmMap: map });
  }

  async function init(handles) {
    api = handles;
    var got = await api.B.storage.local.get(['bmMap', 'bmOn']);
    map = got.bmMap || {}; on = !!got.bmOn;
    rmap = {};
    Object.keys(map).forEach(function (s) { rmap[map[s]] = s; });
    if (on) listen();
    return on;
  }

  function enabled() { return on; }

  async function setEnabled(v) {
    on = !!v;
    await api.B.storage.local.set({ bmOn: on });
    if (on) { listen(); await union(); }
    return on;
  }

  // ---- remote -> here ---------------------------------------------------------------------------

  /* An event arrived. Newest wins, a tombstone removes — but ONLY something this browser previously
   * synced (see the header: a bookmark we never mapped is not ours to delete). */
  async function absorb(id, ev) {
    var cur = items[id];
    if (cur && (cur._at || 0) > (ev.created_at || 0)) return;
    if (!ev.content) {
      items[id] = { removed: true, _at: ev.created_at || 0 };
      if (on) await applyRemoval(id);
      return;
    }
    var obj;
    try { obj = await api.open(ev.content); } catch (_) { return; }   // another key's — not ours
    obj._at = ev.created_at || 0;
    items[id] = obj;
    if (on) await applyUpsert(id, obj);
  }

  async function applyRemoval(id) {
    var bid = map[id];
    if (!bid) return;                       // never synced here — leave the user's tree alone
    writing.add(bid);
    try { await api.B.bookmarks.remove(bid); } catch (_) {}
    setTimeout(function () { writing.delete(bid); }, 2000);
    forget(id);
  }

  async function applyUpsert(id, obj) {
    if (!P.isSyncable(obj)) return;
    var bid = map[id];
    if (bid) {
      var existing = null;
      try { existing = (await api.B.bookmarks.get(bid))[0]; } catch (_) {}
      if (existing) {
        if (existing.title === obj.title && P.normUrl(existing.url) === P.normUrl(obj.url)) return;
        writing.add(bid);
        try { await api.B.bookmarks.update(bid, { title: obj.title || '', url: obj.url }); } catch (_) {}
        setTimeout(function () { writing.delete(bid); }, 2000);
        return;
      }
      forget(id);                            // the mapping is stale — fall through and re-create
    }
    var parent = await ensureFolder(obj.folder || '');
    var made = null;
    try { made = await api.B.bookmarks.create({ parentId: parent, title: obj.title || '', url: obj.url }); }
    catch (_) { return; }
    if (made) { writing.add(made.id); setTimeout(function () { writing.delete(made.id); }, 2000);
                remember(id, made.id); }
  }

  /* Create the folder path if it is missing, under the browser's "other bookmarks" root — which is
   * the one container every browser has and nobody's toolbar layout depends on. Placement is a
   * decision made HERE rather than carried in the event, because the roots have different names and
   * different ids in every browser (see pathOf). */
  async function ensureFolder(path) {
    var roots = await api.B.bookmarks.getTree();
    var kids = (roots[0] && roots[0].children) || [];
    // "other" is conventionally the LAST root in both Firefox and Chrome; fall back to the first.
    var base = (kids[kids.length - 1] || kids[0] || {}).id;
    if (!path) return base;
    var parts = path.split('/').filter(Boolean), cur = base;
    for (var i = 0; i < parts.length; i++) {
      var children = [];
      try { children = await api.B.bookmarks.getChildren(cur); } catch (_) {}
      var hit = children.filter(function (c) { return !c.url && c.title === parts[i]; })[0];
      if (hit) { cur = hit.id; continue; }
      var made = null;
      try { made = await api.B.bookmarks.create({ parentId: cur, title: parts[i] }); } catch (_) { return cur; }
      if (!made) return cur;
      writing.add(made.id); setTimeout(function (id) { return function(){ writing.delete(id); }; }(made.id), 2000);
      cur = made.id;
    }
    return cur;
  }

  // ---- here -> remote --------------------------------------------------------------------------

  async function snapshot() {
    var tree = await api.B.bookmarks.getTree();
    var byId = {}, flat = [];
    (function walk(ns) {
      (ns || []).forEach(function (n) {
        byId[n.id] = n;
        flat.push(n);
        if (n.children) walk(n.children);
      });
    })(tree);
    return flat.filter(P.isSyncable).map(function (n) {
      return { id: n.id, title: n.title || '', url: n.url, folder: P.pathOf(byId, n) };
    });
  }

  async function publishOne(syncId, item) {
    return api.publish(syncId, item);        // background.js owns signing, the outbox and the OK-wait
  }

  async function onLocalChange(browserId) {
    if (!on || writing.has(browserId)) return;      // our own write coming back — never republish
    var node = null;
    try { node = (await api.B.bookmarks.get(browserId))[0]; } catch (_) {}
    if (!node || !P.isSyncable(node)) return;
    var byId = {}, tree = await api.B.bookmarks.getTree();
    (function walk(ns) { (ns || []).forEach(function (n) { byId[n.id] = n; if (n.children) walk(n.children); }); })(tree);
    var syncId = rmap[browserId] || newId();
    var item = { title: node.title || '', url: node.url, folder: P.pathOf(byId, node) };
    remember(syncId, browserId);
    items[syncId] = Object.assign({}, item, { _at: Math.floor(Date.now() / 1000) });
    await publishOne(syncId, item);
  }

  async function onLocalRemove(browserId) {
    if (!on || writing.has(browserId)) return;
    var syncId = rmap[browserId];
    if (!syncId) return;                            // never synced — nothing to tell anyone
    items[syncId] = { removed: true, _at: Math.floor(Date.now() / 1000) };
    forget(syncId);
    await api.publish(syncId, null);                // null = tombstone
  }

  function listen() {
    if (listen._done || !api.B.bookmarks) return;
    listen._done = true;
    var b = api.B.bookmarks;
    b.onCreated.addListener(function (id) { onLocalChange(id); });
    b.onChanged.addListener(function (id) { onLocalChange(id); });
    b.onMoved.addListener(function (id) { onLocalChange(id); });
    b.onRemoved.addListener(function (id) { onLocalRemove(id); });
  }

  /* The first sync, and any manual rebuild: a UNION. Deletes nothing, in either direction. */
  async function union() {
    var local = await snapshot();
    var remote = Object.keys(items).map(function (id) {
      return Object.assign({ id: id }, items[id]);
    });
    var plan = P.planUnion(local, remote, map);
    plan.link.forEach(function (l) { remember(l.syncId, l.browserId); });
    for (var i = 0; i < plan.create.length; i++) await applyUpsert(plan.create[i].id, plan.create[i]);
    for (var j = 0; j < plan.publish.length; j++) {
      var l = plan.publish[j], sid = rmap[l.id] || newId();
      remember(sid, l.id);
      items[sid] = { title: l.title, url: l.url, folder: l.folder, _at: Math.floor(Date.now() / 1000) };
      await publishOne(sid, { title: l.title, url: l.url, folder: l.folder });
    }
    return { created: plan.create.length, published: plan.publish.length,
             linked: plan.link.length, ignoredTombstones: plan.skipRemoved };
  }

  function newId() {
    var b = crypto.getRandomValues(new Uint8Array(16)), s = '';
    for (var i = 0; i < b.length; i++) s += b[i].toString(16).padStart(2, '0');
    return s;
  }

  function count() { return Object.keys(items).filter(function (k) { return !items[k].removed; }).length; }

  P.engine = { init: init, absorb: absorb, setEnabled: setEnabled, enabled: enabled,
               union: union, count: count };
})(typeof self !== 'undefined' ? self : this);
