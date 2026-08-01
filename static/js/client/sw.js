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
const CACHE = 'pc-nostr-v721';
const MEDIA_CACHE = 'pc-media-v2';        // bump → drops the old (possibly poisoned) media cache on activate
const SHARE_CACHE = 'pc-share-v1';        // temporary stash for a file/text shared IN via the OS share sheet
const MEDIA_MAX = 10000;                  // high entry cap (Cache.keys() is insertion-ordered → evict oldest);
                                          // the configurable BYTE budget below is the real limit. Pairs with
                                          // navigator.storage.persist() so the cache isn't evicted by the OS.
const CONFIG_CACHE = 'pc-config-v1';      // client-written config the SW reads (currently: /media-budget bytes)
const VIDEO_MAX_BYTES = 60 * 1024 * 1024; // cache a PLAYED video up to this size (raised 15→60MB for more
                                          // re-watch/bandwidth savings); the 4GB byte budget is the real cap
                                          // and trimMedia evicts oldest, so bigger clips just get cached too.
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
  '/static/js/client/app.js',
  '/static/js/client/news.js',
  '/static/js/client/meme.js',
  '/static/js/client/markets.js',
  '/static/js/client/budget.js',
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
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(()=>{}));
});
self.addEventListener('message', e => { if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting(); });
self.addEventListener('activate', e => {
  // Drop stale shell caches but KEEP the current shell cache AND the media cache (don't re-download
  // every avatar/image just because the app code was redeployed).
  e.waitUntil(caches.keys().then(ks => Promise.all(
    ks.filter(k => k !== CACHE && k !== MEDIA_CACHE && k !== SHARE_CACHE && k !== CONFIG_CACHE).map(k => caches.delete(k))
  )).then(()=>self.clients.claim()));
});

// Stale-while-revalidate for app code (shell + our JS/CSS): serve the CACHED copy instantly so a cold
// start — the phone waking from deep sleep, when the OS has killed the PWA and the radio is slow — paints
// without blocking on the network, then refresh the cache in the background. New builds still reach the
// user: the SW-version bump surfaces the in-app "Update available" prompt (deploys aren't silently
// pinned). First-ever load (cache miss) falls through to the network. This is the cold-start speed win.
function staleWhileRevalidate(req){
  return caches.open(CACHE).then(cache => cache.match(req).then(hit => {
    const net = fetch(req).then(res => { if (res && res.ok) cache.put(req, res.clone()); return res; })
                          .catch(() => hit);   // offline → the cached copy (if any)
    return hit || net;                          // cache HIT → instant paint; MISS → wait for the network
  }));
}
function cacheFirst(req){
  return caches.match(req).then(hit => hit || fetch(req).then(res => {
    const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)).catch(()=>{}); return res;
  }));
}
// Cache-first for media (avatars, images, small played videos): serve from cache with zero network, else
// fetch + store. Bounded so it can't blow the mobile storage quota, and never caches partial/streamed
// video (206) or a big video the user is streaming — only whole, small, actually-fetched clips.
async function cacheFirstMedia(req){
  const cache = await caches.open(MEDIA_CACHE);
  const hit = await cache.match(req);
  if (hit) return hit;
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
    const cacheable = res.status === 200 && (!isVideo || (len > 0 && len <= VIDEO_MAX_BYTES));
    if (cacheable) { await cache.put(req, res.clone()); trimMedia(cache); }
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
async function trimMedia(cache){
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - MEDIA_MAX; i++) cache.delete(keys[i]);   // count cap: evict oldest first
  // Byte cap (configurable): persist() stops the browser evicting under pressure, so bound total storage
  // ourselves. estimate() is origin-wide (a fine proxy) and cheap; over budget → drop the oldest ~10%.
  try {
    if (navigator.storage && navigator.storage.estimate){
      const budget = await mediaBudgetBytes();
      const { usage } = await navigator.storage.estimate();
      if (usage && usage > budget){
        const k2 = await cache.keys();
        const drop = Math.max(1, Math.ceil(k2.length * 0.1));
        for (let i = 0; i < drop; i++) cache.delete(k2[i]);
      }
    }
  } catch (_) {}
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
    if (e.request.destination === 'image' && url.origin !== self.location.origin) e.respondWith(cacheFirstMedia(e.request));
    return;
  }

  // ---- WEB PWA (unchanged: same ordering as before this feature) ----
  if (url.pathname === '/relay' || url.pathname.startsWith('/client/config')) return;  // live data / WS

  const isAppCode = url.pathname === '/client' || url.pathname === '/client/' ||
    url.pathname.startsWith('/static/js/client/') || url.pathname === '/static/css/client.css';
  const isVendorOrIcon = url.pathname.startsWith('/static/vendor/') ||
    /\/static\/(icon-\d+|posterchan-relay|favicon|apple-touch-icon)\.png$/.test(url.pathname);

  if (isAppCode) e.respondWith(staleWhileRevalidate(e.request));
  else if (isVendorOrIcon) e.respondWith(cacheFirst(e.request));
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
  // For a call, if the app is already OPEN + focused it rings itself (and both peers exchange kind-25050
  // during a call, which would otherwise spam the CALLER) — only show the OS notification when nothing's
  // focused (backgrounded / closed), which is exactly the ring-a-closed-app case.
  e.waitUntil(
    (isCall
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
