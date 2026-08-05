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

  /* WHICH top-level container a bookmark lives in. The toolbar is the one people notice: a bookmark
   * put on the toolbar in one browser belongs on the toolbar in the other, and dumping everything into
   * "other bookmarks" quietly loses the arrangement that made it worth syncing.
   *
   * Classified by ROOT ID first, because those are stable and documented — Chrome: '1' bar, '2' other,
   * '3' mobile; Firefox: 'toolbar_____', 'menu________', 'unfiled_____', 'mobile______' — and by title
   * only as a fallback, since titles are localised ("Lesezeichen-Symbolleiste") and a user can rename
   * them. `menu` has no Chrome equivalent, so it lands in `other` there; that is a one-way squash and
   * is why the root travels as a NAME rather than an id.
   */
  var ROOT_IDS = {
    '1': 'toolbar', '2': 'other', '3': 'mobile',                       // Chrome / Edge / Brave
    'toolbar_____': 'toolbar', 'menu________': 'menu',
    'unfiled_____': 'other', 'mobile______': 'mobile',                 // Firefox
  };
  function classifyRoot(node) {
    if (!node) return 'other';
    if (ROOT_IDS[node.id]) return ROOT_IDS[node.id];
    var t = String(node.title || '').toLowerCase();
    if (t.indexOf('toolbar') >= 0 || t.indexOf('bookmarks bar') >= 0 || t.indexOf('favorites bar') >= 0)
      return 'toolbar';
    if (t.indexOf('menu') >= 0) return 'menu';
    if (t.indexOf('mobile') >= 0) return 'mobile';
    return 'other';
  }

  /* The top-level container a node sits under, as one of those names. */
  function rootOf(nodesById, node) {
    var cur = node, last = node, guard = 0;
    while (cur && cur.parentId && guard++ < 64) {
      var next = nodesById[cur.parentId];
      if (!next) break;
      if (!next.parentId) return classifyRoot(cur);        // `cur` IS the top-level container
      last = cur = next;
    }
    return classifyRoot(last);
  }

  /* Drop a leading path segment that is a browser's OWN top-level container.
   *
   * No legitimate folder path starts with one: pathOf strips it, because those names differ per
   * browser and per locale. But events published by the FIRST build of this feature kept it, and
   * those events are still on the relay — so every sync recreated a literal folder called "Other
   * bookmarks" (or "Bookmarks Menu") and filed the bookmark inside it instead of on the toolbar.
   * Deleting the folder locally could never win: the event still said that is where it lives.
   *
   * Applied on the way IN as well as on the way out, because the fix has to work against events
   * already published by a version that is no longer running anywhere. */
  function _isContainerName(name) {
    var t = String(name || '').trim();
    if (!t) return false;
    if (classifyRoot({ id: '', title: t }) !== 'other') return true;      // toolbar / menu / mobile
    return /^(other bookmarks|unfiled bookmarks|bookmarks)$/i.test(t);    // 'other' by name
  }

  /* Where a bookmark belongs: a container NAME plus a path inside it.
   *
   * Two jobs, and the second is why this is not just a strip. No legitimate path starts with a
   * browser's own container — pathOf removes it, because those names differ per browser and locale.
   * But the FIRST build of this feature kept it and published no `root` at all, and those events are
   * still on the relay. Left alone they recreate a literal folder called "Other bookmarks"; merely
   * stripped, every toolbar bookmark ever published by that build lands in "Other" instead, which is
   * exactly the "it synced the folders but not the bookmarks on the toolbar" report.
   *
   * So a leading container segment is CONSUMED, and where the item carries no root it supplies one.
   * Old events land where they were meant to; new ones are unaffected. */
  function placement(item) {
    var parts = String((item && item.folder) || '').split('/').filter(Boolean);
    var root = (item && item.root) || '';
    while (parts.length && _isContainerName(parts[0])) {
      if (!root) root = classifyRoot({ id: '', title: parts[0] });
      parts.shift();
    }
    return { folder: parts.join('/'), root: root || 'other' };
  }

  function cleanFolder(path) { return placement({ folder: path }).folder; }

  /* A bookmark's PLACE, as a comparable string. This is no longer identity — see planUnion, where the
   * URL is — it only disambiguates between several local copies of the same URL: the one already in
   * the same spot is preferred before any other unclaimed copy. Not the title, which gets renamed far
   * more often than a bookmark is re-filed. */
  function matchKey(item) {
    var pl = placement(item);
    return pl.root + '\n' + pl.folder + '\n' + normUrl(item.url);
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
    /* Which remote id OWNS each URL, decided before anything is claimed.
     *
     * On a first enable each browser publishes its own copy of a bookmark BEFORE it has seen the
     * other's — there is nothing to match against yet — so the relay ends up holding two ids for one
     * URL. Every browser then creates the copy it is missing and both end up with two, permanently.
     *
     * One URL, one winner, chosen by the SMALLEST sync id: every browser makes that choice
     * independently and reaches the same answer without coordinating, which is the only way this
     * converges. The losers are computed HERE, before claims are assigned, because a local bookmark
     * still mapped to a losing id must be free to be re-linked to the winner. Working that out after
     * the fact made it worse, not better: the winner found the local copy "claimed", created a
     * THIRD, and dropping the loser's mapping then republished the original under a new id.
     *
     * Nothing is deleted for a superseded id. The bookmark is real; only the extra event is not. */
    var bestByUrl = {};
    remote.forEach(function (r) {
      if (r.removed) return;
      var k = normUrl(r.url);
      var cur = bestByUrl[k];
      if (!cur || String(r.id) < String(cur.id)) bestByUrl[k] = r;
    });
    var loser = {};
    remote.forEach(function (r) {
      if (r.removed) return;
      var win = bestByUrl[normUrl(r.url)];
      if (win && win.id !== r.id) loser[r.id] = true;
    });

    var claimed = {};                                   // browser ids already accounted for
    Object.keys(map).forEach(function (sid) {
      if (!loser[sid]) claimed[map[sid]] = sid;         // a losing id holds no claim on anything
    });

    /* IDENTITY IS THE URL. Location is data ABOUT a bookmark, not what makes it that bookmark, and
     * two browsers almost never agree on it: a link on Chrome's toolbar lives in Firefox's Bookmarks
     * Menu, which has no Chrome equivalent at all. Keying identity on root+folder+url meant those two
     * copies did not match, so each browser created the other's and published its own — two copies
     * everywhere, from one bookmark, with nobody having done anything wrong. That is the duplication
     * between two browsers, and no amount of care further down could have fixed it.
     *
     * Place still disambiguates when it has to: the same URL filed twice on purpose gives several
     * candidates, and the one in the matching spot is preferred before falling back to any unclaimed
     * copy. And a link made across a placement difference does NOT move anything — it records that
     * these are the same bookmark and leaves each browser's arrangement alone. Re-filing somebody's
     * toolbar because another machine keeps it elsewhere is not a sync, it is an opinion. */
    var byUrl = {};
    local.forEach(function (l) {
      var k = normUrl(l.url);
      (byUrl[k] = byUrl[k] || []).push(l);
    });

    var out = { publish: [], create: [], link: [], remove: [], superseded: [], skipRemoved: 0 };

    /* GONE FROM HERE = DELETED HERE. The mapping is what this browser believed the shared state was
     * the last time it looked, so a sync id whose local bookmark has vanished is a deletion that
     * happened while nothing was listening — with sync off, or the browser closed, or before the
     * listeners existed.
     *
     * Without this a union can only ever ADD, so deleting locally and merging RESTORES everything
     * from the relay: "I delete everything on Firefox, click merge, and Firefox gets them back".
     * That is not a sync, it is a one-way download with extra steps. */
    var localIds = {};
    local.forEach(function (l) { localIds[l.id] = true; });
    Object.keys(map).forEach(function (sid) {
      // A superseded id is dropped, not deleted: its bookmark is still here under the winner.
      if (!loser[sid] && !localIds[map[sid]]) out.remove.push(sid);
    });

    var removing = {};
    out.remove.forEach(function (sid) { removing[sid] = true; });

    out.superseded = Object.keys(loser);

    remote.forEach(function (r) {
      if (loser[r.id]) return;                           // a duplicate event for a URL already handled
      if (r.removed) { out.skipRemoved++; return; }      // an existing tombstone is not ours to apply
      /* Deleted here a moment ago — do NOT recreate it. Without this an item lands in BOTH lists:
       * its mapping exists but its local bookmark is gone, so the create pass sees "remote item I do
       * not have" and puts it straight back. The merge then deletes and restores the same bookmark,
       * which is indistinguishable from the deletion never having worked. */
      if (removing[r.id]) return;
      var mapped = map[r.id] && byBrowserId[map[r.id]];
      if (mapped) return;                                // already present and paired
      var cands = (byUrl[normUrl(r.url)] || []).filter(function (c) { return !claimed[c.id]; });
      var exact = cands.filter(function (c) { return matchKey(c) === matchKey(r); })[0];
      var hit = exact || cands[0];
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
    rootOf: rootOf, classifyRoot: classifyRoot, cleanFolder: cleanFolder, placement: placement,
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
  /* Set only once storage has been READ. `api` alone is not enough: it is assigned on the first line
   * of init and the state arrives an await later, and acting in that window is what republished an
   * entire bookmark tree under new ids. */
  var loaded = false;
  /* Ids this engine is currently writing into the tree. The browser fires onCreated/onChanged for
   * OUR OWN writes too, and republishing those is an echo: two devices then bounce one bookmark back
   * and forth, each edit newer than the last, forever. */
  var writing = new Set();
  /* Depth counter for "the engine is writing to the tree right now".
   *
   * `writing` alone is a race it always loses: the browser fires onCreated as part of create()
   * RESOLVING, so the listener runs before the line that registers the new id — and the engine
   * republishes its own write as though the user had made it. That republish is newer than whatever
   * caused it, so it overrides tombstones (a deleted bookmark comes back) and lands on the other
   * browser, which applies it, which fires ITS listeners, and so on: a write storm across the relay
   * and the bookmark database, which is what locked up a browser badly enough to need force-quitting.
   *
   * A flag around the whole apply cannot lose that race — it is set before the first write and held
   * until after the last, with a short tail because an event can arrive a tick late. */
  var applying = 0;
  function beginApply() { applying++; }
  function endApply() { setTimeout(function () { applying = Math.max(0, applying - 1); }, 250); }

  function remember(syncId, browserId) {
    map[syncId] = browserId; rmap[browserId] = syncId;
    api.B.storage.local.set({ bmMap: map });
  }
  function forget(syncId) {
    var b = map[syncId];
    delete map[syncId];
    /* ONLY clear the reverse entry if it still points back to US. When the relay holds several ids for
     * one URL (older versions' random-id duplicates), a merge maps the winning id onto the bookmark
     * and then forgets the losing id — and both briefly named the SAME browser bookmark. Deleting
     * rmap[b] unconditionally then wiped the WINNER's reverse mapping, leaving the bookmark with no
     * sync id: deleting it found nothing to tombstone, so the stale event resurrected it on the next
     * sync. "Brave brings back everything I delete." */
    if (b && rmap[b] === syncId) delete rmap[b];
    api.B.storage.local.set({ bmMap: map });
  }

  /* WHAT IT KNOWS IS PERSISTED, and that is not an optimisation.
   *
   * Chrome kills an idle service worker within ~30s, so a popup opened a minute later ran against an
   * engine that had forgotten every bookmark — "0 synced", on a browser that had just synced dozens.
   * Worse than the wrong number: with `items` empty, a "Merge now" sees no remote state, so nothing
   * is deduped and everything is published again.
   *
   * The vault caches its DECRYPTED items for exactly this reason, and the argument is the same here:
   * this lives in extension storage right next to the vault key, so caching ciphertext instead would
   * protect nothing while making every popup wait on a relay. */
  async function init(handles) {
    api = handles;
    var got = await api.B.storage.local.get(['bmMap', 'bmOn', 'bmItems', 'bmPending']);
    map = got.bmMap || {}; on = !!got.bmOn;
    items = got.bmItems || {};
    pendingRemovals = got.bmPending || 0;      // a bulk-delete confirm survives a popup close / SW restart
    loaded = true;
    rmap = {};
    Object.keys(map).forEach(function (s) { rmap[map[s]] = s; });
    _reindex();                                // build the URL index from the restored items, once
    if (on) listen();
    return on;
  }

  var _saveT = null;
  function saveSoon() {
    clearTimeout(_saveT);
    _saveT = setTimeout(function () {
      try { api.B.storage.local.set({ bmItems: items }); } catch (_) {}
    }, 400);
  }

  function enabled() { return !!api && loaded && on; }

  async function setEnabled(v) {
    // Never throw out of a message handler: the popup shows what comes back, and an exception from
    // here reads as "cannot access property B of null" instead of the actual situation.
    if (!api || !loaded) throw new Error('bookmark sync is not ready yet — reopen this in a moment');
    on = !!v;
    await api.B.storage.local.set({ bmOn: on });
    if (on) { listen(); await union(); }
    else { pendingRemovals = 0; try { await api.B.storage.local.set({ bmPending: 0 }); } catch (_) {} }
    return on;
  }

  // The count a bulk-delete is waiting to confirm, 0 when there is nothing pending. Read by the popup
  // so it can offer the confirm on OPEN — the reason you no longer have to keep the popup open.
  function pending() { return pendingRemovals || 0; }

  // ---- remote -> here ---------------------------------------------------------------------------

  /* EVENTS ARE ABSORBED ONE AT A TIME. The socket's onmessage fires absorb() for every EVENT WITHOUT
   * awaiting it, so a relay dumping its backlog (worse still, one left polluted with thousands of
   * duplicate events by older versions) runs thousands of absorbs CONCURRENTLY. Two things break at
   * once, and both were the user's daily pain:
   *   - the browser locks up / crashes: thousands of simultaneous bookmarks.create/get calls flood the
   *     bookmark backend (in Firefox, the parent process) faster than it can serve them;
   *   - it DUPLICATES: two events for one URL run in parallel, each checks the map, neither has mapped
   *     the bookmark yet, so BOTH create it — the dedupe can only work if it sees the first one's
   *     mapping, which needs the first to finish first.
   * A promise chain serialises them: fired concurrently, applied in order, one at a time. A failure of
   * one must not wedge the queue. (A single-engine sim never caught this — it awaited each absorb.) */
  var _absorbChain = Promise.resolve();
  function absorb(id, ev) {
    var run = _absorbChain.then(function () { return _absorb(id, ev); },
                                function () { return _absorb(id, ev); });
    _absorbChain = run.catch(function () {});
    return run;
  }

  /* An event arrived. Newest wins, a tombstone removes — but ONLY something this browser previously
   * synced (see the header: a bookmark we never mapped is not ours to delete). */
  async function _absorb(id, ev) {
    if (!api || !loaded) return;
    var cur = items[id];
    /* >=, NOT >. A relay re-sends everything on every connection, and a replaceable event that
     * arrives again carries the SAME created_at — so a strict > let every one of them fall through
     * to a decrypt and a full re-apply (a get, a folder lookup, a comparison) for a bookmark that
     * had not changed. On a real library that is the whole collection re-applied on every reconnect,
     * per relay, on the browser's UI thread, forever. Nothing is lost by ignoring it: an event with
     * the same address and the same timestamp is the same event, and a genuine edit gets a newer one.
     * Enabling sync does not depend on this path either — setEnabled() merges from what is already
     * stored. */
    if (cur && (cur._at || 0) >= (ev.created_at || 0)) return;
    if (!ev.content) {
      // Keep the URL we last knew for this id: applyRemoval checks the local bookmark still MATCHES
      // it before removing anything. Dropping it here would leave nothing to verify against.
      items[id] = { removed: true, _at: ev.created_at || 0, url: (cur && cur.url) || '' };
      saveSoon();
      if (on) await applyRemoval(id);
      return;
    }
    var obj;
    try { obj = await api.open(ev.content); } catch (_) { return; }   // another key's — not ours
    obj._at = ev.created_at || 0;
    items[id] = obj;
    _index(id, obj.url);
    saveSoon();
    if (on) await applyUpsert(id, obj);
  }

  /* Remove the local bookmark a tombstone names — ONLY when it is still the same bookmark.
   *
   * This cost somebody their Firefox tree once. The guard then was "only remove what THIS browser
   * previously synced", which was intact and not enough: an earlier bug republished everything under
   * fresh sync ids, so every browser mapped its OWN real bookmarks onto those ids, and cleaning up
   * the duplicates on one machine published tombstones that were "legitimate" for bookmarks they had
   * never been about. The rule reasoned about an identity a previous bug had already corrupted.
   *
   * So identity is no longer taken on trust. The URL we last held for this id must still match the
   * URL of the bookmark in the tree; if it does not, the mapping is stale or wrong and the answer is
   * to forget it, never to delete. A confused id can then cost a link between two records — which
   * re-links on the next merge — instead of somebody's bookmarks.
   *
   * (The republish that poisoned the ids came from a union running before the engine had loaded its
   * state. That is now refused outright — see `loaded` — which is what makes this safe to turn back
   * on rather than merely smaller.) */
  async function applyRemoval(id) {
    beginApply(); try { return await _applyRemoval(id); } finally { endApply(); }
  }
  async function _applyRemoval(id) {
    var bid = map[id];
    if (!bid) return;                       // never synced here — not ours to touch
    var knew = (items[id] && items[id].url) || '';
    var node = null;
    try { node = (await api.B.bookmarks.get(bid))[0]; } catch (_) {}
    if (!node) { forget(id); return; }      // already gone
    if (!knew || P.normUrl(node.url) !== P.normUrl(knew)) {
      forget(id);                           // not the bookmark this tombstone is about
      return;
    }
    writing.add(bid);
    try { await api.B.bookmarks.remove(bid); } catch (_) {}
    setTimeout(function () { writing.delete(bid); }, 2000);
    forget(id);
  }

  /* A URL -> [ids] INDEX, maintained incrementally, so finding every event for a URL is O(1) instead of
   * a scan of the whole `items` map. Without it, the first sync of a relay left polluted by older
   * versions (thousands of duplicate events) ran `_idsForUrl` — an O(items) scan — for EVERY event,
   * which is O(n²): measured at 62 SECONDS for 900 URLs × 3 duplicates, on the UI thread. That is the
   * browser locking up while it syncs, all over again. The index is append-only; `_idsForUrl` filters
   * out ids whose item is now a tombstone, so a stale entry costs nothing but a skip. */
  var _byUrl = {};          // normUrl -> [id, id, …]
  function _index(id, url) {
    if (!url) return;
    var k = P.normUrl(url), a = _byUrl[k] || (_byUrl[k] = []);
    if (a.indexOf(id) < 0) a.push(id);
  }
  function _reindex() {
    _byUrl = {};
    Object.keys(items).forEach(function (i) { var it = items[i]; if (it && it.url) _index(i, it.url); });
  }
  function _idsForUrl(url) {
    var a = _byUrl[P.normUrl(url)] || [], out = [];
    for (var i = 0; i < a.length; i++) { var it = items[a[i]]; if (it && !it.removed) out.push(a[i]); }
    return out;
  }

  async function applyUpsert(id, obj) {
    beginApply(); try { return await _applyUpsert(id, obj); } finally { endApply(); }
  }
  async function _applyUpsert(id, obj) {
    if (!P.isSyncable(obj)) return;
    var bid = map[id];
    if (bid) {
      var existing = null;
      try { existing = (await api.B.bookmarks.get(bid))[0]; } catch (_) {}
      if (existing) {
        var _p2 = P.placement(obj);
        var want = await ensureFolder(_p2.folder, _p2.root);
        var sameText = existing.title === obj.title && P.normUrl(existing.url) === P.normUrl(obj.url);
        var samePlace = existing.parentId === want;
        if (sameText && samePlace) return;
        writing.add(bid);
        // MOVES COUNT. Applying only the text left a bookmark dragged to the toolbar on one device
        // sitting wherever it already was on every other one — which is most of what "sync my
        // bookmarks" is asked to do.
        try { if (!sameText) await api.B.bookmarks.update(bid, { title: obj.title || '', url: obj.url }); } catch (_) {}
        try { if (!samePlace) await api.B.bookmarks.move(bid, { parentId: want }); } catch (_) {}
        setTimeout(function () { writing.delete(bid); }, 2000);
        return;
      }
      forget(id);                            // the mapping is stale — fall through and re-create
    }
    /* ONE LOCAL BOOKMARK PER URL, however many events the relay holds for it.
     *
     * Older versions published each bookmark under a fresh RANDOM id, so a single URL can have many
     * live events on the relay. Without this, absorbing each of them creates ANOTHER copy of the same
     * bookmark — that is the duplicates the user sees, and the pile of create() calls is the browser
     * locking up on sync. Worse, deleting the visible copy only tombstones the id it was mapped to, so
     * the next stale event recreates it: "Brave brings back everything I delete". If a live local
     * bookmark for this URL already exists under another id, link nothing and create nothing — the
     * existing one IS this bookmark, and the delete path (below) tombstones every id for the URL. */
    var siblings = _idsForUrl(obj.url);
    for (var s = 0; s < siblings.length; s++) {
      var other = siblings[s];
      // A live local bookmark for this URL already exists under another id → do NOT create a second.
      // Trusting `map` here rather than a per-event bookmarks.get() is the whole point: the get() ran
      // ~twice per duplicate URL and, across a polluted relay, was thousands of bookmark-API calls that
      // froze the browser. A stale mapping (bookmark deleted out from under us) is the rare case, and
      // it is reconciled by the mapping check at the top of this function and by the next merge — never
      // worth an API round-trip on every single incoming event.
      if (String(other) !== String(id) && map[other]) return;
    }
    var _pl = P.placement(obj);
    var parent = await ensureFolder(_pl.folder, _pl.root);
    /* The URL may already be a LOCAL bookmark this engine has not mapped yet — the SAME bookmark
     * created independently in the other browser (a default like "Poster-Chan" present in both), or a
     * local publish still in flight. The items/map check above cannot see it: it is not on the relay
     * under a known id yet, and the local onCreated → publish is debounced, so absorb of the other
     * browser's copy races ahead and creates a SECOND one. That is the duplicate two browsers make out
     * of one bookmark. ONE search of the live tree catches it — adopt the existing bookmark instead of
     * creating a duplicate. Only runs when about to create (the cheap items/map dedup handles the flood
     * of re-delivered duplicate events first), so it is not the per-event get()-storm that was removed. */
    try {
      var hits = await api.B.bookmarks.search({ url: obj.url });
      var twin = (hits || []).filter(function (n) { return n.url && P.normUrl(n.url) === P.normUrl(obj.url); })[0];
      if (twin) { remember(id, twin.id); return; }
    } catch (_) {}
    var made = null;
    try { made = await api.B.bookmarks.create({ parentId: parent, title: obj.title || '', url: obj.url }); }
    catch (_) { return; }
    if (made) { writing.add(made.id); setTimeout(function () { writing.delete(made.id); }, 2000);
                remember(id, made.id); }
  }

  /* Resolve a folder path to a local id, creating what is missing — ONE level at a time, memoised,
   * and never twice concurrently.
   *
   * THIS IS WHERE THE DUPLICATE FOLDERS CAME FROM. Events arrive from the subscription in a burst and
   * each one is applied independently, so twenty bookmarks in "Work" all ran this at once: every one
   * of them called getChildren, none of them saw a "Work" folder because none had been created yet,
   * and every one created its own. The check and the create are not atomic, and nothing was making
   * them so — the result is one duplicate per concurrent event, which is exactly what a first sync
   * of a real bookmark tree looks like.
   *
   * The fix is a per-(parent,name) promise: the first caller creates, everyone else awaits the SAME
   * promise and gets the same id. Memoised per level rather than per full path, or "Work/A" and
   * "Work/B" would still race on creating "Work".
   *
   * The root itself travels as a NAME (toolbar / menu / other) because ids and titles differ per
   * browser; resolving it is a decision made here, on arrival. `menu` has no Chrome equivalent and
   * falls back to `other`. */
  var _folder = {};        // parentId + '\n' + name -> Promise<id>

  /* The top-level container ids, read ONCE.
   *
   * getTree() serialises the WHOLE bookmark tree across the extension boundary, and this used to run
   * for every arriving bookmark — twice, via ensureFolder. Measured at 603 full-tree reads for 300
   * bookmarks: quadratic work whose cost is invisible on the ten-node trees a test uses and which,
   * on a real profile, is the browser locking up while it syncs.
   *
   * The roots are fixed for the life of a profile — Chrome's '1'/'2'/'3', Firefox's toolbar_____ and
   * friends — so they are cached for the session. Nothing else here needs the whole tree. */
  var _roots = null;
  async function rootId(root) {
    if (!_roots) {
      var roots = await api.B.bookmarks.getTree();
      var kids = (roots[0] && roots[0].children) || [];
      var byRoot = {};
      kids.forEach(function (k) { byRoot[P.classifyRoot(k)] = k.id; });
      byRoot._fallback = (kids[kids.length - 1] || kids[0] || {}).id;
      _roots = byRoot;
    }
    return _roots[root || 'other'] || _roots.other || _roots.toolbar || _roots._fallback;
  }

  function ensureChild(parentId, name) {
    var key = parentId + '\n' + name;
    if (_folder[key]) return _folder[key];
    _folder[key] = (async function () {
      var children = [];
      try { children = await api.B.bookmarks.getChildren(parentId); } catch (_) {}
      var hit = children.filter(function (c) { return !c.url && c.title === name; })[0];
      if (hit) return hit.id;
      var made = null;
      try { made = await api.B.bookmarks.create({ parentId: parentId, title: name }); } catch (_) {}
      if (!made) return parentId;                      // could not create — put it in the parent
      writing.add(made.id);
      var id = made.id;
      setTimeout(function () { writing.delete(id); }, 2000);
      return id;
    })();
    // A failure must not be remembered as an answer, or every later bookmark inherits it.
    _folder[key].catch(function () { delete _folder[key]; });
    return _folder[key];
  }

  async function ensureFolder(path, root) {
    var cur = await rootId(root);
    var parts = String(path || '').split('/').filter(Boolean);
    for (var i = 0; i < parts.length; i++) cur = await ensureChild(cur, parts[i]);
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
      return { id: n.id, title: n.title || '', url: n.url,
               folder: P.pathOf(byId, n), root: P.rootOf(byId, n) };
    });
  }

  async function publishOne(syncId, item) {
    return api.publish(syncId, item);        // background.js owns signing, the outbox and the OK-wait
  }

  /* Local edits are BATCHED, and that is a performance requirement, not tidiness.
   *
   * Publishing a bookmark needs its PATH, which means reading the tree — and the browser fires one
   * event per bookmark. Restoring a backup, importing an HTML file or dragging in a folder therefore
   * used to cost one full-tree serialisation per bookmark, each one walking the whole tree again,
   * plus a publish that blocks the next event behind it. That is the other half of "the browser goes
   * unresponsive while it syncs", and it is the worse half, because it fires on the user's own bulk
   * actions — exactly when the tree is at its biggest.
   *
   * A burst collapses into one tree read and one pass. The wait also coalesces the create-then-move
   * pair a drag produces, so a dragged bookmark is published once, at its final location. */
  var pendingLocal = [], localTimer = null;
  var LOCAL_BATCH_MS = 400;
  function onLocalChange(browserId) {
    if (!on || applying > 0 || writing.has(browserId)) return;      // our own write coming back — never republish
    if (pendingLocal.indexOf(browserId) < 0) pendingLocal.push(browserId);
    if (localTimer) clearTimeout(localTimer);
    localTimer = setTimeout(function () {
      localTimer = null;
      flushLocal().catch(function (e) { try { console.warn('[pcai] bookmark publish failed', e); } catch (_) {} });
    }, LOCAL_BATCH_MS);
  }

  async function flushLocal() {
    if (!on || !pendingLocal.length) return;
    var ids = pendingLocal; pendingLocal = [];
    var byId = {}, tree = await api.B.bookmarks.getTree();       // ONCE for the whole burst
    (function walk(ns) { (ns || []).forEach(function (n) { byId[n.id] = n; if (n.children) walk(n.children); }); })(tree);
    var dirty = false;
    for (var i = 0; i < ids.length; i++) {
      // ONE BAD ONE MUST NOT TAKE THE BATCH DOWN. Before batching, each event was published on its
      // own and a failure cost that bookmark only; letting a throw escape the loop would silently
      // drop every bookmark queued behind it — worst on exactly the bulk imports this exists for.
      try {
      var browserId = ids[i];
      if (writing.has(browserId)) continue;            // became ours while it waited
      var node = byId[browserId];                      // gone from the tree = removed; onLocalRemove has it
      if (!node || !P.isSyncable(node)) continue;
      var syncId = rmap[browserId] || await idFor(node.url);
      var item = { title: node.title || '', url: node.url,
                   folder: P.pathOf(byId, node), root: P.rootOf(byId, node) };
      remember(syncId, browserId);                     // identity first, and permanently
      var ok = await publishOne(syncId, item);
      /* Only record it as KNOWN-ON-THE-RELAY when the relay said so. Recording it regardless is what
       * makes a read-only pairing (or a dropped socket) look synced forever: union() dedupes against
       * this map, so an entry that was never published would never be retried. The mapping above is
       * kept either way — a retry must reuse the same sync id, or it publishes a duplicate. */
      if (ok) { items[syncId] = Object.assign({}, item, { _at: Math.floor(Date.now() / 1000) }); _index(syncId, item.url); dirty = true; }
      } catch (_) { /* keep going: the next merge retries anything that never reached the relay */ }
    }
    if (dirty) saveSoon();
  }

  /* A bookmark removed here becomes a tombstone, so the other devices drop it too — carrying the URL
   * it had, which is what lets the receiving side check it is removing the right thing. */
  async function tombstoneOne(syncId) {
    var knew = items[syncId] || {};
    items[syncId] = { removed: true, _at: Math.floor(Date.now() / 1000), url: knew.url || '' };
    saveSoon();
    forget(syncId);
    await api.publish(syncId, null);                // null = tombstone
  }

  /* Delete EVERY event for this URL, not just the one this browser had mapped.
   *
   * The relay can hold several live events for one URL (older versions republished under random ids).
   * Tombstoning only the mapped id leaves the others alive, and the next sync recreates the bookmark
   * from one of them — "Brave brings back everything I delete". Killing every id for the URL is what
   * makes a delete actually stick. Ids are collected BEFORE tombstoning, since tombstoneOne removes
   * each from the live set as it goes. */
  async function tombstoneWithSiblings(syncId) {
    var url = (items[syncId] && items[syncId].url) || '';
    var ids = url ? _idsForUrl(url) : [];
    if (ids.indexOf(syncId) < 0) ids.push(syncId);
    for (var i = 0; i < ids.length; i++) { try { await tombstoneOne(ids[i]); } catch (_) {} }
  }

  async function onLocalRemove(browserId) {
    var q = pendingLocal.indexOf(browserId);
    if (q >= 0) pendingLocal.splice(q, 1);          // queued publish for a node that no longer exists
    /* `applying` — not `writing` — is the guard for "the engine is removing this, not the user":
     * every engine removal (an incoming tombstone via applyRemoval, or tidy) runs inside beginApply.
     * `writing` is a DIFFERENT thing: it parks the id of a node the engine just CREATED, for 2s, to
     * swallow the onCreated echo. A folder the engine auto-creates as it syncs sits in `writing` for
     * those 2s — and the old code, by also gating removals on `writing`, threw away a real user
     * deletion of that folder if it happened inside the window. So gate only on `applying` here. */
    if (!on || applying > 0) return;
    if (!writing.has(browserId)) {
      var syncId = rmap[browserId];
      if (syncId) {
        /* DELETING A DUPLICATE IS NOT DELETING THE BOOKMARK. If another LOCAL copy of this URL still
         * exists, the user removed one of two duplicates — tombstoning the URL here would delete it on
         * every OTHER device while this browser keeps its surviving copy ("I deleted the dupe and now
         * it's only on Brave"). So when a copy survives, just drop this id's mapping and let the
         * survivor keep the URL synced; only when NOTHING is left locally is it a real deletion that
         * tombstones every relay copy of the URL. */
        var url = (items[syncId] && items[syncId].url) || '';
        var survivor = null;
        if (url) { try {
          var hits = await api.B.bookmarks.search({ url: url });
          survivor = (hits || []).filter(function (n) {
            return n.id !== browserId && !writing.has(n.id) && n.url && P.normUrl(n.url) === P.normUrl(url); })[0];
        } catch (_) {} }
        if (survivor) {
          forget(syncId);
          if (!rmap[survivor.id]) { remember(syncId, survivor.id); onLocalChange(survivor.id); }
          return;
        }
        await tombstoneWithSiblings(syncId); return;   // no copy left — kill every relay copy of this URL
      }
    }

    /* We reach here for either an UNMAPPED node (a folder — folders are never synced as items) or a
     * node still parked in `writing` from a recent auto-create that the user has now deleted.
     *
     * Deleting a folder fires ONE onRemoved, for the folder — the browser does NOT fire onRemoved for
     * each bookmark inside it. So the child bookmarks' tombstones would never be published, and
     * deleting a folder of links silently failed to sync until the user happened to press "Merge now".
     * That is exactly "I deleted a folder and it's still on my other browser". Reconcile the tree
     * against the map and tombstone every mapped bookmark that just vanished. Debounced, so deleting a
     * folder of a hundred links is ONE tree read, not a hundred. The sweep reads the CURRENT map, so
     * it can never tombstone a bookmark the engine still holds — only ones genuinely gone from the tree. */
    scheduleRemovalSweep();
  }

  /* A live removal can orphan mapped bookmarks (a deleted folder takes its children with it, silently).
   * Sweep the tree once and tombstone any mapped bookmark that is no longer in it. Only ever triggered
   * by a real onRemoved — a restore or re-pair fires onCreated, never onRemoved, so this cannot mistake
   * "everything is being rebuilt" for "everything was deleted" (that ambiguity is the union's problem,
   * and is why the wholesale-loss guard lives there and not here). */
  var _sweepT = null;
  function scheduleRemovalSweep() {
    if (!on) return;
    if (_sweepT) clearTimeout(_sweepT);
    _sweepT = setTimeout(function () {
      _sweepT = null;
      sweepRemovals().catch(function (e) { try { console.warn('[pcai] bookmark removal sweep failed', e); } catch (_) {} });
    }, LOCAL_BATCH_MS);
  }

  async function sweepRemovals() {
    if (!on) return;
    var present = {}, tree = await api.B.bookmarks.getTree();     // ONCE for the whole burst
    (function walk(ns) { (ns || []).forEach(function (n) { present[n.id] = true; if (n.children) walk(n.children); }); })(tree);
    var gone = [];
    Object.keys(rmap).forEach(function (bid) { if (!present[bid]) gone.push(rmap[bid]); });
    for (var i = 0; i < gone.length; i++) {
      var sid = gone[i];
      if (!map[sid]) continue;                       // already tombstoned by the direct path or a prior sweep
      // ONE FAILURE MUST NOT STRAND THE REST — same reasoning as flushLocal's per-item guard.
      // Siblings, so a deleted folder full of links that each have stale duplicate events on the relay
      // has every copy killed, not just the mapped one.
      try { await tombstoneWithSiblings(sid); } catch (_) {}
    }
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
  var pendingRemovals = 0;

  /* Merges run ONE AT A TIME, and that is a correctness rule as much as a performance one.
   *
   * union() is triggered by a relay's EOSE — which fires once PER RELAY, again on every reconnect,
   * and again from the 30-second connect check — so several could be in flight at once. Each one
   * reads the whole tree, plans against a map the others are still mutating, and publishes what the
   * others have not finished recording: duplicates, and a pile of full-tree reads landing on the
   * browser's UI thread at the same moment (in Firefox the bookmarks API is served by the parent
   * process, so that is the window lagging, not just the extension).
   *
   * Queued rather than dropped: a caller that asked to merge gets a merge, and the popup's Merge
   * button still resolves with its own result. */
  var chain = Promise.resolve();
  function union(opts) {
    var run = chain.then(function () { return _union(opts); },
                         function () { return _union(opts); });
    chain = run.catch(function () {});          // one failure must not wedge every later merge
    return run;
  }

  async function _union(opts) {
    opts = opts || {};
    if (!api || !loaded) throw new Error('bookmark sync is not ready yet — reopen this in a moment');
    var local = await snapshot();
    var remote = Object.keys(items).map(function (id) {
      return Object.assign({ id: id }, items[id]);
    });
    var plan = P.planUnion(local, remote, map);
    plan.link.forEach(function (l) { remember(l.syncId, l.browserId); });
    // A superseded id is a duplicate EVENT, not a duplicate bookmark: drop the mapping so this
    // browser tracks one id per URL, and leave the bookmark alone — it belongs to the winner now.
    (plan.superseded || []).forEach(function (sid) { if (map[sid]) forget(sid); });

    /* A deletion here becomes a tombstone there — unless it looks like a RESTORE rather than a
     * decision. A profile reset, a backup restore or a re-paired browser all present as "everything I
     * had mapped is gone", and publishing that would delete the same bookmarks on every other device:
     * the failure this feature has already caused once. Past the threshold it refuses and REPORTS,
     * and the popup offers to go ahead — so deleting everything on purpose still works, deliberately.
     */
    var removals = plan.remove.slice();
    var bulk = removals.length > Math.max(5, Math.round(local.length * 0.5));
    if (bulk && !opts.confirmRemovals) {
      pendingRemovals = removals.length;
      // PERSIST it, so reopening the popup can offer the confirm again. Without this the confirm lived
      // only in the popup's memory and died the instant the popup lost focus — so the user had to keep
      // the popup open to finish a delete, and clicking away silently reset it to "Merge now".
      try { api.B.storage.local.set({ bmPending: pendingRemovals }); } catch (_) {}
    } else {
      pendingRemovals = 0;
      try { api.B.storage.local.set({ bmPending: 0 }); } catch (_) {}
      for (var d = 0; d < removals.length; d++) {
        // tombstoneWithSiblings, NOT a single tombstone: a URL can have several live events on the relay
        // (older versions' random-id duplicates). Killing only the one mapped id here left the duplicate
        // alive, and it resurrected the bookmark on the next sync — so "Delete N everywhere" never stuck
        // and the "N missing" prompt came back no matter how many times it was confirmed. This is the
        // SAME rule the live-delete path and the folder sweep already use; the merge-confirm path had
        // been missed.
        await tombstoneWithSiblings(removals[d]);
      }
      saveSoon();
    }
    for (var i = 0; i < plan.create.length; i++) await applyUpsert(plan.create[i].id, plan.create[i]);
    var sent = plan.publish.length;
    for (var j = 0; j < plan.publish.length; j++) {
      var l = plan.publish[j], sid = rmap[l.id] || await idFor(l.url);
      remember(sid, l.id);
      var body = { title: l.title, url: l.url, folder: l.folder, root: l.root };
      var ok = await publishOne(sid, body);
      if (ok) { items[sid] = Object.assign({}, body, { _at: Math.floor(Date.now() / 1000) }); _index(sid, body.url); saveSoon(); }
      else sent--;                                     // report what actually left, not what was tried
    }
    // When there was something to send and none of it went, say WHY rather than reporting a bare 0.
    var blocked = (plan.publish.length && !sent && api.why) ? api.why() : '';
    return { created: plan.create.length, published: sent, wanted: plan.publish.length,
             linked: plan.link.length, ignoredTombstones: plan.skipRemoved, blocked: blocked,
             removed: pendingRemovals ? 0 : removals.length, pendingRemovals: pendingRemovals };
  }

  /* THE SYNC ID IS DERIVED FROM THE URL, and that is the whole answer to duplication.
   *
   * It used to be random. Two browsers enabling sync both publish their copy of the same bookmark
   * before either has seen the other's — there is nothing to match against yet — so the relay ended
   * up holding TWO events for one bookmark, and every browser created the copy it was missing. No
   * amount of matching afterwards fixes that: the duplicate events already exist, and three separate
   * attempts to reconcile them after the fact made it worse (two copies, then three, then eight).
   *
   * A derived id means both browsers compute the SAME `d` tag for the same bookmark without ever
   * coordinating, so the relay — where these are replaceable events — keeps exactly one. The
   * duplicate cannot be created, rather than being detected and cleaned up.
   *
   * The consequence is deliberate: one URL is one synced bookmark. Changing a bookmark's URL is an
   * add plus a delete, which is what it is anyway. */
  async function idFor(url) {
    var data = new TextEncoder().encode('pcai-bm:' + P.normUrl(url));
    var buf = await crypto.subtle.digest('SHA-256', data);
    var b = new Uint8Array(buf), s = '';
    for (var i = 0; i < 16; i++) s += b[i].toString(16).padStart(2, '0');
    return s;
  }

  /* Merge sibling folders that share a title, keeping the first and moving everything into it.
   *
   * For duplicates ALREADY created before folder creation was serialised: a check-then-create race
   * made one copy per concurrent event, so a first sync of a real tree produced dozens. Nothing here
   * can tell "a folder this engine duplicated" from "two folders somebody named the same on
   * purpose", so it is a BUTTON and never automatic.
   *
   * It only moves children and deletes a folder that is EMPTY after the move. No bookmark is removed
   * by this in any branch. */
  async function tidy() {
    beginApply(); try { return await _tidy(); } finally { endApply(); }
  }
  async function _tidy() {
    if (!api) throw new Error('bookmark sync is not ready yet');
    var merged = 0, removed = 0;

    async function pass(parentId) {
      var kids = [];
      try { kids = await api.B.bookmarks.getChildren(parentId); } catch (_) { return; }
      var byTitle = {};
      for (var i = 0; i < kids.length; i++) {
        var k = kids[i];
        if (k.url) continue;                              // a bookmark, never a duplicate folder
        var t = k.title || '';
        if (!byTitle[t]) { byTitle[t] = k.id; continue; }
        var keep = byTitle[t], move = [];
        try { move = await api.B.bookmarks.getChildren(k.id); } catch (_) {}
        for (var j = 0; j < move.length; j++) {
          writing.add(move[j].id);
          try { await api.B.bookmarks.move(move[j].id, { parentId: keep }); } catch (_) {}
          (function (id) { setTimeout(function () { writing.delete(id); }, 2000); })(move[j].id);
        }
        var left = [];
        try { left = await api.B.bookmarks.getChildren(k.id); } catch (_) {}
        if (!left.length) {
          writing.add(k.id);
          try { await api.B.bookmarks.remove(k.id); removed++; } catch (_) {}
        }
        merged++;
      }
      // Recurse AFTER merging, so the surviving folder is walked once with everything in it.
      var after = [];
      try { after = await api.B.bookmarks.getChildren(parentId); } catch (_) {}
      for (var m = 0; m < after.length; m++) if (!after[m].url) await pass(after[m].id);
    }

    var roots = await api.B.bookmarks.getTree();
    var tops = (roots[0] && roots[0].children) || [];
    for (var r = 0; r < tops.length; r++) await pass(tops[r].id);
    _folder = {};                                         // ids may have moved under us
    return { merged: merged, removed: removed };
  }

  function count() { if (!api) return 0; return Object.keys(items).filter(function (k) { return !items[k].removed; }).length; }

  P.engine = { init: init, absorb: absorb, setEnabled: setEnabled, enabled: enabled,
               union: union, count: count, tidy: tidy, pending: pending };
})(typeof self !== 'undefined' ? self : this);
