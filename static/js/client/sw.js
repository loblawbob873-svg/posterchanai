/* PosterChan Nostr PWA service worker.
 * App code (our JS/CSS + the /client shell) is served STALE-WHILE-REVALIDATE: the cached copy paints
 * instantly (fast cold start when the phone wakes from deep sleep on a slow radio) while a fresh copy is
 * fetched in the background; a new build reaches the user via the in-app "Update available" prompt (the
 * SW-version bump), so deploys aren't silently pinned.
 * The large vendor bundle + icons are cache-first (they rarely change; bump CACHE to refresh).
 * Media (same-origin images + small played videos) is cache-first in a SEPARATE cache (MEDIA_CACHE)
 * that survives shell bumps. NOTE: we only cache responses we can TRUST (a real 200) — never an OPAQUE
 * cross-origin response, whose status is masked to 0, so an avatar host's 404/blip would be stored as
 * "valid" and served forever, breaking that avatar on every later view (the "no avatars" bug). Opaque
 * third-party avatars still load fresh via the browser's own HTTP cache, which already dedupes them. */
const CACHE = 'pc-nostr-v1019';
const MEDIA_CACHE = 'pc-media-v2';        // bump → drops the old (possibly poisoned) media cache on activate
// Content-addressed blobs fetched by JS rather than by an element: the ENCRYPTED DRIVE — Notes
// attachments, music tracks, an offloaded note body, the files index. They land in their OWN cache,
// not MEDIA_CACHE, because the two evict on completely different terms: the timeline's images are a
// firehose that would push a deliberately-imported note library out within a session, and the
// library is the thing the app promises is readable offline ("open this note once while online").
const DRIVE_CACHE = 'pc-drive-v1';
const DRIVE_MAX = 6000;                   // entry cap; the shared byte budget below is the real limit
const DRIVE_MAX_BYTES = 8 * 1024 * 1024;  // per blob. 96 MB was far too generous: a few large
                                          // attachments exhaust the origin's storage quota, and it
                                          // is the many SMALL ones that make reopening a note fast.
                                          // A bigger file still loads, just from the network.
const SHARE_CACHE = 'pc-share-v1';        // temporary stash for a file/text shared IN via the OS share sheet
const MEDIA_MAX = 10000;                  // high entry cap (Cache.keys() is insertion-ordered → evict oldest);
                                          // the configurable BYTE budget below is the real limit. Pairs with
                                          // navigator.storage.persist() so the cache isn't evicted by the OS.
const CONFIG_CACHE = 'pc-config-v1';      // client-written config the SW reads (currently: /media-budget bytes)
const VIDEO_MAX_BYTES = 60 * 1024 * 1024; // cache a PLAYED video up to this size (raised 15→60MB for more
                                          // re-watch/bandwidth savings); the 4GB byte budget is the real cap
                                          // and trimCache evicts oldest, so bigger clips just get cached too.
// The bundled Capacitor APK registers this SW at the ROOT (/sw.js); the web PWA registers it at
// /client/sw.js. In the APK the client is the LOCAL BUNDLE — authoritative and updated by the APK
// itself — so the SW must be MEDIA-ONLY there and never cache/serve app code or navigations, or it
// would pin stale JS across an APK update. Detect app-mode from our own script path.
const IS_APP = self.location.pathname === '/sw.js';
const SHELL = [
  '/client',
  '/static/css/client.css',
  '/static/vendor/nostr/nostr.bundle.js',
  '/static/js/client/sprite.js',
  '/static/js/client/store.js',
  '/static/js/client/negentropy.js',
  '/static/js/client/relay.js',
  '/static/js/client/outbox.js',
  '/static/js/client/qr.js',
  '/static/js/client/urlclean.js',
  '/static/js/client/app.js',
  '/static/js/client/news.js',
  '/static/js/client/websearch.js',
  '/static/js/client/ical.js',
  '/static/js/client/calendar.js',
  '/static/js/client/vcard.js',
  '/static/js/client/contacts.js',
  '/static/js/client/os.js',
  '/static/js/client/stats.js',
  '/static/js/client/meme.js',
  '/static/js/client/markets.js',
  '/static/js/client/budget.js',
  '/static/js/client/joplin.js',
  '/static/js/client/fs-android.js',
  '/static/js/client/foldersync.js',
  '/static/js/client/syncrun.js',
  '/static/js/client/sync.js',
  '/static/js/client/notes.js',
  '/static/js/client/vaultcore.js',
  '/static/js/client/vault.js',
  '/static/js/client/chess.js',
  '/static/js/client/ttt.js',
  '/static/js/client/hangman.js',
  '/static/js/client/connect4.js',
  '/static/js/client/blackjack.js',
  '/static/js/client/holdem.js',
  '/static/js/client/signer-worker.js',
  '/static/posterchan-relay.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', e => {
  // App is media-only (the bundle serves the shell), and SHELL lists '/client' which 404s in the app —
  // an atomic addAll would reject anyway. So only precache the shell in the web PWA.
  if (IS_APP) return;
  // Activate IMMEDIATELY rather than sit in a "waiting" state that the page has to coax out by posting
  // SKIP_WAITING. That message hand-off was unreliable in some Firefox PWAs — the worker never activated,
  // so "Update available" showed forever. Self-activating here is robust; the client reloads onto the new
  // build on controllerchange (rate-limited so it can't thrash). Drafts autosave on pagehide → reload is safe.
  self.skipWaiting();
  // Add entries INDIVIDUALLY. addAll is atomic, so ONE bad path (a module renamed without updating SHELL)
  // rejects the whole batch and precaches NOTHING — a failure that is invisible until someone goes offline,
  // which is the one moment it matters. Per-entry, a bad path costs only that entry.
  e.waitUntil(caches.open(CACHE).then(c => Promise.all(SHELL.map(u => c.add(u).catch(()=>{})))));
});
self.addEventListener('message', e => {
  if (e.data && e.data.type === 'SKIP_WAITING') { self.skipWaiting(); return; }
  if (e.data && e.data.type === 'pc-dl-open') _dlOpen(e.data, e.ports && e.ports[0]);
});

/* ---- streamed downloads -------------------------------------------------------------------
 * A file the page GENERATES, written to disk without ever being held in memory.
 *
 * showSaveFilePicker is the obvious way and it is Chromium-only, so on Firefox the Notes backup had
 * a choice between assembling gigabytes as one Blob and splitting into many downloads — and the
 * second is worse than it sounds, because browsers block the 2nd and later automatic downloads, so
 * parts of the archive go missing with nothing said.
 *
 * Instead the page opens a channel here, navigates to a URL this worker owns, and posts the bytes
 * through as it makes them; the response is a stream the browser writes straight to disk with a
 * Content-Disposition filename. One file, any size, constant memory, and it works everywhere a
 * service worker does.
 *
 * Deliberately on its OWN path (/client/__dl/<id>) so it cannot interact with any other rule here,
 * and the id is random so nothing else can address a stream it did not create. */
const _dls = new Map();          // id -> { name, stream }

function _dlOpen(msg, port){
  if (!msg.id || !port) return;
  let ctrl = null;
  const stream = new ReadableStream({
    start(c){ ctrl = c; },
    // The user cancelled the download, or the tab went away: stop the producer.
    cancel(){ try { port.postMessage({ cancelled: true }); } catch (_) {} _dls.delete(msg.id); },
  });
  _dls.set(msg.id, { name: msg.name || 'download.bin', type: msg.mime || 'application/octet-stream', stream });
  port.onmessage = (ev) => {
    const d = ev.data || {};
    try {
      if (d.chunk) {
        ctrl.enqueue(new Uint8Array(d.chunk));
        // Backpressure, so a fast producer cannot make the worker hold the whole file after all:
        // the page waits for this before sending the next chunk.
        port.postMessage({ want: Math.max(0, ctrl.desiredSize || 0) });
      } else if (d.end) {
        ctrl.close();
      }
    } catch (_) { _dls.delete(msg.id); }
  };
  try { port.start(); } catch (_) {}
  // Nothing is fetched yet — the page navigates to the URL once it has this reply.
  try { port.postMessage({ ready: true }); } catch (_) {}
}
self.addEventListener('activate', e => {
  // Drop stale shell caches but KEEP the current shell cache AND the media cache (don't re-download
  // every avatar/image just because the app code was redeployed).
  e.waitUntil(caches.keys().then(ks => Promise.all(
    ks.filter(k => k !== CACHE && k !== MEDIA_CACHE && k !== DRIVE_CACHE && k !== SHARE_CACHE && k !== CONFIG_CACHE).map(k => caches.delete(k))
  )).then(()=>self.clients.claim()));
});

// Stale-while-revalidate for app code (shell + our JS/CSS): serve the CACHED copy instantly so a cold
// start — the phone waking from deep sleep, when the OS has killed the PWA and the radio is slow — paints
// without blocking on the network, then refresh the cache in the background. New builds still reach the
// user: the SW-version bump surfaces the in-app "Update available" prompt (deploys aren't silently
// pinned). First-ever load (cache miss) falls through to the network. This is the cold-start speed win.
//
// Matching is EXACT FIRST, {ignoreSearch:true} only as the offline last resort, and the order matters in
// both directions.
//
// Why ignoreSearch is needed at all: client.html requests its assets as `/static/js/client/app.js?v=<mtime>`
// (the cache-busting token _static_version() stamps on the page) while SHELL above lists the BARE paths.
// Cache.match is exact-URL INCLUDING the query, so a plain match could never answer a single real request
// — the entire precache was dead weight. Offline worked only because staleWhileRevalidate had separately
// stored the versioned URL during an earlier ONLINE visit, so install-then-go-offline failed outright.
//
// Why it must NOT be the primary match: a UI-only deploy bumps `?v=` but deliberately does NOT bump CACHE
// (that is the whole point of shipping static changes without a restart). Matching loosely would then hand
// back the PREVIOUS build's app.js for the new page — and since no new service worker installed, nothing
// fires controllerchange and the "Update available" prompt never appears, so the deploy would silently
// need a second reload to show up. Exact-first keeps a version bump a genuine cache miss, exactly as
// before; the loose match only ever runs once the network has already failed, where stale beats nothing.
//
// _put then prunes same-path/different-query siblings on every write, so the loose fallback can only ever
// find ONE copy per path — otherwise every deploy would leave another stale build behind, and match()
// returns the first insertion-ordered hit, i.e. the oldest. (It also drops the bare precached copy once
// the versioned one arrives; that is the intended handover, not a loss.)
function _stale(cache, req){ return cache.match(req, { ignoreSearch: true }); }
async function _put(cache, req, res){
  await cache.put(req, res);
  try {
    const url = new URL(req.url);
    for (const k of await cache.keys()){
      const u = new URL(k.url);
      if (u.origin === url.origin && u.pathname === url.pathname && u.search !== url.search) await cache.delete(k);
    }
  } catch (_) {}
}
function staleWhileRevalidate(req){
  return caches.open(CACHE).then(async cache => {
    const exact = await cache.match(req);
    const net = fetch(req).then(res => { if (res && res.ok) _put(cache, req, res.clone()); return res; })
                          .catch(() => exact || _stale(cache, req));   // offline → this version, else any
    if (exact){ net.catch(()=>{}); return exact; }        // right build cached → instant paint, refresh behind
    // MISS + offline leaves `net` resolving to undefined, and respondWith(undefined) is a TypeError that
    // surfaces as the browser's own network-error page — the dino, from inside a "working" service worker.
    return (await net) || Response.error();
  });
}
function cacheFirst(req){
  return caches.open(CACHE).then(async cache => {
    const exact = await cache.match(req);
    if (exact) return exact;
    try {
      const res = await fetch(req);
      if (res && res.ok) _put(cache, req, res.clone());
      return res;
    } catch (_) {
      return (await _stale(cache, req)) || Response.error();
    }
  });
}
// SPA navigations (the installed app's launch, a manifest shortcut, the share target, an OAuth return).
// Cached under the STABLE key '/client' regardless of the query the navigation carried, because the route
// renders the same shell for all of them — it takes no query params, the SPA reads location.search itself.
// Without this, /client?compose=1, ?view=notifications, ?view=messages and ?shared=1 — i.e. every
// home-screen shortcut and the OS share sheet, the most app-like entry points there are — were exact-match
// cache misses and so the ONLY parts of the app guaranteed to fail offline.
function shellDoc(req){
  return caches.open(CACHE).then(cache => cache.match('/client').then(hit => {
    const net = fetch(req).then(res => { if (res && res.ok) cache.put('/client', res.clone()); return res; })
                          .catch(() => hit);
    return hit || net.then(r => r || Response.error());
  }));
}
// /client/config: relay_url, blossom_url, admin npubs, branding. It used to bypass the SW entirely, so
// offline it degraded to `{}` and the client did not even know which relay to reconnect TO when the radio
// came back. Network-first (it must never go stale while online) with a cache fallback so a cold offline
// boot still gets the last-known config.
function networkFirst(req){
  return caches.open(CACHE).then(cache =>
    fetch(req).then(res => { if (res && res.ok) cache.put(req, res.clone()); return res; })
              .catch(() => cache.match(req).then(hit => hit || Response.error())));
}
// Cache-first for media (avatars, images, small played videos): serve from cache with zero network, else
// fetch + store. Bounded so it can't blow the mobile storage quota, and never caches partial/streamed
// video (206) or a big video the user is streaming — only whole, small, actually-fetched clips.
async function cacheFirstMedia(req){
  // Guarded for the same reason as cacheFirstBlob: a storage failure here would take every avatar
  // and image on the page down with it, and a cache is never allowed to do that.
  let cache = null;
  try {
    cache = await caches.open(MEDIA_CACHE);
    const hit = await cache.match(req);
    if (hit) return hit;
  } catch (_) { cache = null; }
  let res;
  // Cross-origin images (fediverse avatars + custom emoji, other instances' media) come back OPAQUE via the
  // <img>'s default no-cors mode → status 0 → the guard below refuses to cache them, so they re-download on
  // every view. Try a CORS fetch first: fedi media is almost always CDN-served with Access-Control-Allow-
  // Origin:* → a real 200 we CAN cache. Fall back to the normal (opaque) fetch when the host sends no CORS,
  // so it still displays (just uncached). credentials:'omit' — never send our cookies to third-party hosts.
  try {
    if (req.destination === 'image' && new URL(req.url).origin !== self.location.origin) {
      const c = await fetch(req.url, { mode: 'cors', credentials: 'omit' });
      if (c && c.status === 200) res = c;
    }
  } catch (_) {}
  if (!res) { try { res = await fetch(req); } catch (_) { return Response.error(); } }  // net died + nothing cached → <img> onerror → LOGO
  try {
    const isVideo = req.destination === 'video';
    const len = +(res.headers.get('content-length') || 0);
    // ONLY cache a trusted 200 (same-origin / CORS). An opaque cross-origin response has status 0 — we
    // can't tell success from an error page, so caching it risks poisoning the avatar permanently. Skip
    // it; the browser's HTTP cache still handles repeat loads of the same avatar URL.
    const cacheable = cache && res.status === 200 && (!isVideo || (len > 0 && len <= VIDEO_MAX_BYTES));
    if (cacheable) { await cache.put(req, res.clone()); trimCache(cache, MEDIA_MAX, 'media'); }
  } catch (_) {}   // quota exceeded / uncacheable → just serve it uncached
  return res;
}
const MEDIA_DEFAULT_BUDGET = 4 * 1024 * 1024 * 1024;   // 4 GB default (was 1.5) — user-configurable in
                                                       // Settings → Media cache; the client writes the chosen
                                                       // byte value to CONFIG_CACHE/media-budget, read below.
async function mediaBudgetBytes(){
  try { const c = await caches.open(CONFIG_CACHE); const r = await c.match('/media-budget');
    if (r){ const n = +(await r.text()); if (n > 0) return n; } } catch (_) {}
  return MEDIA_DEFAULT_BUDGET;
}
/* Bound ONE cache: an entry cap, then the configurable origin-wide byte budget. Shared by the media
 * and drive caches — the eviction policy is a single rule about this origin's storage, and keeping a
 * second copy of it per cache is how the two would end up disagreeing about the same number.
 *
 * THROTTLED, because the drive cache is written in BURSTS (opening one imported note stores dozens of
 * attachments back to back) where the media cache trickles. Each run enumerates up to MEDIA_MAX
 * Requests and calls storage.estimate(), an origin-wide computation of tens of milliseconds — running
 * that per stored blob competed with the decrypts it was meant to be speeding up. Nothing here is
 * urgent: it is a budget guard, not a correctness one. */
const _trimAt = {};
async function trimCache(cache, max, key){
  const now = Date.now();
  if (now - (_trimAt[key] || 0) < 20000) return;
  // The byte budget below is ORIGIN-WIDE (storage.estimate), so watching 4 GB of timeline video
  // would otherwise make the next note attachment evict the note library — the opposite of why the
  // two caches are separate. The drive answers to its entry cap and its per-blob cap; only the
  // media cache trims on the shared byte budget.
  const byBytes = key === 'media';
  _trimAt[key] = now;
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - max; i++) cache.delete(keys[i]);   // count cap: evict oldest first
  // Byte cap (configurable): persist() stops the browser evicting under pressure, so bound total storage
  // ourselves. estimate() is origin-wide (a fine proxy); over budget → drop the oldest ~10%.
  try {
    if (byBytes && navigator.storage && navigator.storage.estimate){
      const budget = await mediaBudgetBytes();
      const { usage } = await navigator.storage.estimate();
      if (usage && usage > budget){
        const drop = Math.max(1, Math.ceil(keys.length * 0.1));
        for (let i = 0; i < drop; i++) cache.delete(keys[i]);   // `keys` is still the insertion order
      }
    }
  } catch (_) {}
}

/* ---- Encrypted drive: content-addressed blobs ----
 * A Blossom blob is addressed BY THE SHA256 OF ITS BYTES, so the URL can never mean anything else —
 * cache-first with no revalidation is not a heuristic here, it is exact.
 *
 * These are invisible to every rule above. `encFileUrl()` reads them with fetch(), whose
 * request.destination is '' (not 'image' — the bytes are ciphertext; the <img> only ever sees the
 * decrypted object: URL), so they fell through to the pass-through branch and were NEVER stored.
 * Opening a note re-downloaded every picture in it, every time, and "open this note once while
 * online" bought nothing at all. */
/* Content-addressed by the LAST path segment, not by our own mount path. `/blossom/<sha>` is only
 * where THIS node serves them: encFileUrl and trackUrl both fetch `mediaServer() + '/' + sha`, and
 * mediaServer() is the user's OWN server root whenever they've set one — so a rule anchored to
 * /blossom/ would have matched nothing for exactly those users and left the cache silently inert,
 * with "open this note once while online" quietly untrue for them.
 *
 * `mode !== 'navigate'` so a 64-hex route can never be pinned cache-first as a page. */
function isDriveBlob(url, req){
  return /\/[0-9a-f]{64}(\.[a-z0-9]{1,8})?$/i.test(url.pathname)
    && req.mode !== 'navigate' && req.destination !== 'image' && req.destination !== 'video';
}
async function cacheFirstBlob(req){
  /* THE PAGE'S RESPONSE IS NEVER DERIVED FROM ANYTHING THIS FUNCTION TOUCHES.
   *
   * This cache has now broken reading attachments twice in one day, in two different ways, and both
   * times it presented as "the file is gone" rather than "the cache is unwell":
   *
   *   1. caches.open()/match() outside a try — under storage pressure their rejection escaped
   *      respondWith() and every blob failed.
   *   2. the served Response was rebuilt from a buffer but carried the ORIGINAL headers, so its
   *      declared Content-Length described the bytes on the wire rather than the bytes in the body.
   *
   * Both were caused by the response the page receives being something this code constructed. So it
   * no longer constructs one. On a miss the ORIGINAL fetch Response is returned untouched — byte for
   * byte what the page would get with no service worker at all — and the cached copy is fetched
   * SEPARATELY, in the background, for small blobs only.
   *
   * That costs one extra download of a ≤8 MB blob the first time it is seen, and buys an invariant
   * worth more than the saving: no future mistake in here can corrupt or block a read, because the
   * read does not pass through here. A cache is an optimisation; it does not get to be load-bearing.
   */
  let hit = null;
  try {
    const cache = await caches.open(DRIVE_CACHE);
    hit = await cache.match(req);
  } catch (_) { hit = null; }             // storage unavailable: straight to the network
  if (hit) return hit;

  const res = await fetch(req);           // rejects like any fetch would; nothing swallows it
  _cacheBlobLater(req);
  return res;
}

/* Fetch a second copy, purely to store it. Deliberately separate from the request the page is
 * waiting on. Bounded by the same rules as before (200, octet-stream, no Range, size cap) and by a
 * small queue, so opening a note with dozens of attachments cannot put dozens of extra downloads in
 * flight at once. */
const _blobQueue = [];
let _blobFetching = 0;
function _cacheBlobLater(req){
  if (req.headers.get('range')) return;
  if (_blobQueue.length > 60) return;                       // a burst is not worth unbounded memory
  _blobQueue.push(req.url);
  _pumpBlobCache();
}
async function _pumpBlobCache(){
  if (_blobFetching >= 2 || !_blobQueue.length) return;
  const url = _blobQueue.shift();
  _blobFetching++;
  try {
    const cache = await caches.open(DRIVE_CACHE);
    if (!(await cache.match(url))) {
      const r = await fetch(url);
      const ct = (r.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
      const len = +(r.headers.get('content-length') || 0);
      const opaque = !ct || ct === 'application/octet-stream' || ct === 'binary/octet-stream';
      // A missing Content-Type is NOT treated as ciphertext here: a third-party media host that
      // omits it would otherwise get its JSON listing frozen in a cache-first store forever.
      if (r.status === 200 && ct && opaque && len > 0 && len <= DRIVE_MAX_BYTES) {
        await cache.put(url, r);          // r is consumed by the cache; nothing else reads it
        trimCache(cache, DRIVE_MAX, 'drive');
      }
    }
  } catch (_) {}                          // any failure: this blob is simply not cached
  finally {
    _blobFetching--;
    if (_blobQueue.length) _pumpBlobCache();
  }
}

// Web Share Target (POST): another app shared a file/text INTO us via the OS share sheet. The browser
// POSTs the multipart form here. A not-yet-open client can't receive a File directly, so stash the
// file(s) + text in a temporary cache and redirect to /client?shared=1 — the app reads the stash on boot
// (see _consumeSharedFiles), opens the composer with the text, and uploads the files. Then clears it.
async function handleShare(request){
  try {
    const fd = await request.formData();
    const files = fd.getAll('media').filter(f => f && f.size);
    const meta = { title: fd.get('title') || '', text: fd.get('text') || '', url: fd.get('url') || '', n: files.length };
    const cache = await caches.open(SHARE_CACHE);
    await cache.put('/__share_meta', new Response(JSON.stringify(meta), { headers: { 'content-type': 'application/json' } }));
    for (let i = 0; i < files.length; i++){
      await cache.put('/__share_file_' + i, new Response(files[i], {
        headers: { 'content-type': files[i].type || 'application/octet-stream', 'x-name': encodeURIComponent(files[i].name || ('file' + i)) },
      }));
    }
  } catch (_) {}
  return Response.redirect('/client?shared=1', 303);
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method === 'POST' && url.pathname === '/client/share'){ e.respondWith(handleShare(e.request)); return; }
  if (e.request.method !== 'GET') return;

  // APK: MEDIA-ONLY, and only CROSS-ORIGIN IMAGES (avatars + uploaded images from the server / other
  // hosts). Deliberately NOT: same-origin bundle assets (they must refresh on an APK update, so never
  // cache them), videos (their cross-origin range requests come back opaque → don't proxy, leave
  // playback direct), app code, or navigations. Everything else passes straight through, keeping the
  // bundle authoritative so the SW can't fight the APK's own update flow.
  if (IS_APP){
    // The encrypted drive is the one exception worth making: it is DATA, not app code, addressed by
    // the hash of its own bytes, and in the bundle it is cross-origin (the SW runs on localhost, the
    // blobs come from the instance) so nothing else here would ever store it. Without this the app
    // re-downloads a note's attachments on every open and Notes is unusable offline.
    if (isDriveBlob(url, e.request)){ e.respondWith(cacheFirstBlob(e.request)); return; }
    if (e.request.destination === 'image' && url.origin !== self.location.origin) e.respondWith(cacheFirstMedia(e.request));
    return;
  }

  // A generated file being written to disk. Checked before anything else: it is ours, it exists
  // only in memory, and it must never fall through to the network.
  if (url.pathname.startsWith('/client/__dl/')) {
    const d = _dls.get(url.pathname.slice('/client/__dl/'.length));
    if (d) {
      _dls.delete(url.pathname.slice('/client/__dl/'.length));
      e.respondWith(new Response(d.stream, { headers: {
        'Content-Type': d.type,
        'Content-Disposition': 'attachment; filename="' + d.name.replace(/["\\]/g, '') + '"',
        'Cache-Control': 'no-store',
      }}));
      return;
    }
  }

  // ---- WEB PWA ----
  if (url.pathname === '/relay') return;                                                          // live WS
  if (url.pathname.startsWith('/client/config')){ e.respondWith(networkFirst(e.request)); return; }

  // Launching the app (or a shortcut / share / OAuth return) — answer from the shell doc, query and all.
  if (e.request.mode === 'navigate' && (url.pathname === '/client' || url.pathname === '/client/')){
    e.respondWith(shellDoc(e.request)); return;
  }

  const isAppCode = url.pathname === '/client' || url.pathname === '/client/' ||
    url.pathname.startsWith('/static/js/client/') || url.pathname === '/static/css/client.css';
  const isVendorOrIcon = url.pathname.startsWith('/static/vendor/') ||
    /\/static\/(icon-\d+|posterchan-relay|favicon|apple-touch-icon)\.png$/.test(url.pathname);

  if (isAppCode) e.respondWith(staleWhileRevalidate(e.request));
  else if (isVendorOrIcon) e.respondWith(cacheFirst(e.request));
  // Encrypted-drive blobs (fetch(), so destination ''), BEFORE the destination rules below: a public
  // <img> pointing at the same path is ordinary media and keeps going to MEDIA_CACHE.
  else if (isDriveBlob(url, e.request)) e.respondWith(cacheFirstBlob(e.request));
  // CROSS-ORIGIN VIDEO **AND AUDIO**: leave it to the browser, exactly as the APK branch above does.
  //
  // A <video>/<audio> with no crossorigin attribute fetches no-cors, so fetch() hands back an OPAQUE
  // response (status 0). cacheFirstMedia can never store one — its guard requires status 200 — so
  // intercepting these bought NOTHING, and cost a failure mode: an opaque body cannot satisfy the Range
  // requests a media element makes. Chromium tolerates that; FIREFOX fails the load outright with
  // MEDIA_ERR_SRC_NOT_SUPPORTED, surfaced as "No video with supported format and MIME type found".
  //
  // That is why a twimg clip played in the desktop app but not in Firefox: the app's SW is root-scoped
  // (IS_APP) and already skips video, while the web SW proxied it. Same-origin video still goes through
  // the cache below — it comes back transparent, so it is both cacheable and range-able.
  //
  // AUDIO was left out of that fix and hit the catch-all `fetch(e.request)` at the bottom, which is the
  // same proxy with the same opaque result. The symptom is the same failure wearing different words:
  // Firefox reports NS_ERROR_DOM_MEDIA_DECODE_ERR / "FFmpeg audio error" and the track is silent. It
  // showed up on the Meme Builder's voice-over layers — a talk clip's speech is an <audio> on
  // media.poster.place, i.e. cross-origin — where the export had sound and the preview did not. The
  // file was never the problem: those .wav are canonical PCM (verified byte by byte — RIFF/WAVE, fmt 16,
  // format 1, mono 24kHz/16-bit, data chunk exactly matching the file length) and ffmpeg decodes them
  // clean. Nothing about audio makes it a better candidate for proxying than video.
  else if ((e.request.destination === 'video' || e.request.destination === 'audio')
           && url.origin !== self.location.origin) return;
  // Avatars + images always; videos only get stored if played + small (see cacheFirstMedia).
  else if (e.request.destination === 'image' || e.request.destination === 'video') e.respondWith(cacheFirstMedia(e.request));
  else e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));  // everything else
});

/* ---- Web Push: show OS notifications when the app is closed, focus/open it on click. ---- */
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: (e.data && e.data.text()) || 'New activity' }; }
  const title = d.title || 'PosterChan';
  const isCall = d.type === 'call';
  const opts = {
    body: d.body || 'New activity',
    icon: '/static/icon-192.png', badge: '/static/icon-192.png',
    tag: isCall ? ('call-' + (d.author || '')) : (d.eid || undefined),   // collapse dup pushes
    data: d,
    vibrate: isCall ? [400, 200, 400, 200, 400] : [40, 30, 40],
    requireInteraction: isCall,     // an incoming call stays up until you act on it
    renotify: isCall || undefined,
  };
  // Calls and DMs are suppressed while a window is focused; everything else always shows.
  //
  // Calls: the open app rings itself, and peers exchange kind-25050 all call long, which would
  // otherwise spam the CALLER.
  // DMs: NIP-17 requires the sender to publish a SECOND gift wrap addressed to THEMSELVES, so their
  // own outgoing message is p-tagged to their own key and looks exactly like an incoming one to a
  // server that cannot decrypt either. Without this check, sending a message notifies you about it —
  // on the very tab you typed it in. (A second device that is closed still buzzes; distinguishing
  // that would need a marker on the wrap, and tagging the outside of a gift wrap is the metadata
  // leak NIP-17 exists to avoid.)
  // Reminders join them: an open app already shows its own full-screen reminder pop-up and beep, so
  // an OS notification on top is the same alert twice.
  const suppressIfFocused = isCall || d.type === 'dm' || d.type === 'reminder';
  e.waitUntil(
    (suppressIfFocused
      ? clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
          if (cs.some(c => c.focused || c.visibilityState === 'visible')) return;
          return self.registration.showNotification(title, opts);
        })
      : self.registration.showNotification(title, opts))
  );
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  // The app runs at '/', the web PWA at '/client'. Focus the existing window (the app has just one), else
  // open the right home URL for the context.
  const home = IS_APP ? '/' : '/client';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    for (const c of cs) { if ((IS_APP || c.url.includes('/client')) && 'focus' in c) return c.focus(); }
    return clients.openWindow(home);
  }));
});
