/* PosterChan Nostr PWA service worker. Caches the app shell + static assets so it installs and
 * launches offline; network-first for /client/config (live data), cache-first for static. */
const CACHE = 'pc-nostr-v1';
const SHELL = [
  '/client',
  '/static/css/client.css',
  '/static/vendor/nostr/nostr.bundle.js',
  '/static/js/client/store.js',
  '/static/js/client/relay.js',
  '/static/js/client/app.js',
  '/static/js/client/signer-worker.js',
  '/static/posterchan-relay.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(()=>self.skipWaiting()).catch(()=>{}));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // never cache websocket/relay or the live config
  if (url.pathname === '/relay' || url.pathname.startsWith('/client/config')) return;
  // app shell + our static: cache-first, fall back to network and update cache
  if (url.pathname.startsWith('/static/') || url.pathname === '/client' || url.pathname === '/client/') {
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)).catch(()=>{}); return res;
    }).catch(()=>hit)));
  }
  // everything else (blossom media etc): network, fall back to cache
  else {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
  }
});
