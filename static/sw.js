// Service Worker for Poster-chan AI PWA
//
// This app is NOT offline-capable and is not pretending to be: chat, image generation, TTS and search are
// all round trips to this server, so there is no cached state that would make the UI usable without one.
// What it owes the user offline is an ANSWER instead of the browser's network-error page — hence
// OFFLINE_URL below. (The Nostr client at /client is the offline-capable half; it has its own worker at
// /client/sw.js, whose narrower scope wins for every page under /client.)
const CACHE_NAME = 'posterchanai-v75';
const OFFLINE_URL = '/static/offline.html';
const STATIC_ASSETS = [
  OFFLINE_URL,
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
  '/manifest.json'
];

self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .catch(err => console.error('SW install failed:', err))
  );
});

/* THIS WORKER MUST ONLY DELETE ITS OWN CACHES.
 *
 * CacheStorage is per-ORIGIN, not per-scope, and this worker shares an origin with the Nostr client's
 * (`/client/sw.js`, scope `/client/`). A bare "delete everything that is not CACHE_NAME" therefore
 * reaches straight across and destroys the client's shell (`pc-nostr-vNNN`), its media cache, the
 * encrypted drive's blobs, and `pc-webxdc-v1` — which holds the downloaded mini apps, up to 178 MB
 * for the Half-Life port, re-fetched byte-for-byte identical because they are content-addressed.
 * The client's worker had the mirror-image bug and was deleting `posterchanai-vNN` right back, so a
 * user who used both surfaces re-downloaded one of them every time either version bumped.
 *
 * Nothing said so; each app just got slow once in a while. Scoped by PREFIX so the two workers stop
 * fighting, and so a cache added on either side is not silently collected by the other.
 * Guarded by tests/test_webxdc_gallery.py::SwCacheKeepList. */
const CACHE_PREFIX = 'posterchanai-';

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames
          .filter(name => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
          .map(name => caches.delete(name))
      );
    })
  );
  return self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Skip non-GET requests
  if (event.request.method !== 'GET') return;

  // Skip API calls and WebSockets - let the browser handle them normally.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws')) return;

  // Navigations: always the network (nothing here is servable from cache), but when it fails, answer with
  // our own offline card instead of letting Chromium render the dino. Never cache the response — these are
  // server-rendered, per-user, CSRF-bearing pages, and a stale one is worse than no page at all.
  if (event.request.mode === 'navigate') {
    // …except into /client, which is the offline-capable app and owns that scope. On a browser that has
    // been to / but not yet to /client, this root worker would otherwise answer that first navigation —
    // and offline it would serve the card below, whose own "Open the Nostr client" link lands right back
    // here. Passing through lets /client's worker take over the moment it is registered.
    if (url.pathname === '/client' || url.pathname.startsWith('/client/')) return;
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match(OFFLINE_URL).then(hit => hit || Response.error()))
    );
    return;
  }

  // For static assets only - cache first, then network
  // NEVER cache these files - they must always be fresh:
  // - csrf.js (CSRF tokens)
  // - chat.js (main app logic)
  // - *.css (styling updates)
  // - *.html (template changes)
  if (url.pathname.includes('csrf.js') ||
      url.pathname.includes('chat.js') ||
      url.pathname.endsWith('.css') ||
      url.pathname.endsWith('.html')) {
    return; // Don't cache, always fetch fresh
  }
  
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.json') {
    event.respondWith(
      caches.match(event.request).then(cached => {
        if (cached) return cached;
        return fetch(event.request).then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          }
          return response;
        }).catch(() => cached);
      })
    );
    return;
  }
});
