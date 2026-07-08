/* PosterChan Nostr PWA service worker.
 * App code (our JS/CSS + the /client shell) is served NETWORK-FIRST so deploys reach users
 * immediately (cache is only an offline fallback) — caching it cache-first served stale code.
 * The large vendor bundle + icons are cache-first (they rarely change; bump CACHE to refresh).
 * Media (same-origin images + small played videos) is cache-first in a SEPARATE cache (MEDIA_CACHE)
 * that survives shell bumps. NOTE: we only cache responses we can TRUST (a real 200) — never an OPAQUE
 * cross-origin response, whose status is masked to 0, so an avatar host's 404/blip would be stored as
 * "valid" and served forever, breaking that avatar on every later view (the "no avatars" bug). Opaque
 * third-party avatars still load fresh via the browser's own HTTP cache, which already dedupes them. */
const CACHE = 'pc-nostr-v179';
const MEDIA_CACHE = 'pc-media-v2';        // bump → drops the old (possibly poisoned) media cache on activate
const MEDIA_MAX = 500;                    // entry cap; Cache.keys() is insertion-ordered → evict oldest
const VIDEO_MAX_BYTES = 15 * 1024 * 1024; // only cache a PLAYED video if it's small; stream big ones
const SHELL = [
  '/client',
  '/static/css/client.css',
  '/static/vendor/nostr/nostr.bundle.js',
  '/static/js/client/store.js',
  '/static/js/client/negentropy.js',
  '/static/js/client/relay.js',
  '/static/js/client/app.js',
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
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(()=>self.skipWaiting()).catch(()=>{}));
});
self.addEventListener('activate', e => {
  // Drop stale shell caches but KEEP the current shell cache AND the media cache (don't re-download
  // every avatar/image just because the app code was redeployed).
  e.waitUntil(caches.keys().then(ks => Promise.all(
    ks.filter(k => k !== CACHE && k !== MEDIA_CACHE).map(k => caches.delete(k))
  )).then(()=>self.clients.claim()));
});

function networkFirst(req){
  return fetch(req).then(res => {
    const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)).catch(()=>{});
    return res;
  }).catch(() => caches.match(req));
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
  try { res = await fetch(req); }
  catch (_) { return Response.error(); }   // network died + nothing cached → let the <img> onerror → LOGO
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
async function trimMedia(cache){
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - MEDIA_MAX; i++) cache.delete(keys[i]);   // evict oldest first
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.pathname === '/relay' || url.pathname.startsWith('/client/config')) return;  // live data / WS

  const isAppCode = url.pathname === '/client' || url.pathname === '/client/' ||
    url.pathname.startsWith('/static/js/client/') || url.pathname === '/static/css/client.css';
  const isVendorOrIcon = url.pathname.startsWith('/static/vendor/') ||
    /\/static\/(icon-\d+|posterchan-relay|favicon|apple-touch-icon)\.png$/.test(url.pathname);

  if (isAppCode) e.respondWith(networkFirst(e.request));
  else if (isVendorOrIcon) e.respondWith(cacheFirst(e.request));
  // Avatars + images always; videos only get stored if played + small (see cacheFirstMedia). With
  // preload="none" on timeline videos, a video isn't even fetched until the user taps play.
  else if (e.request.destination === 'image' || e.request.destination === 'video') e.respondWith(cacheFirstMedia(e.request));
  else e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));  // everything else
});

/* ---- Web Push: show OS notifications when the app is closed, focus/open it on click. ---- */
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: (e.data && e.data.text()) || 'New activity' }; }
  const title = d.title || 'PosterChan';
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || 'New activity',
    icon: '/static/icon-192.png', badge: '/static/icon-192.png',
    tag: d.eid || undefined,        // collapse duplicate pushes for the same event
    data: d, vibrate: [40, 30, 40],
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    for (const c of cs) { if (c.url.includes('/client') && 'focus' in c) return c.focus(); }
    return clients.openWindow('/client');
  }));
});
