/* PosterChan Nostr PWA service worker.
 * App code (our JS/CSS + the /client shell) is served NETWORK-FIRST so deploys reach users
 * immediately (cache is only an offline fallback) — caching it cache-first served stale code.
 * The large vendor bundle + icons are cache-first (they rarely change; bump CACHE to refresh). */
const CACHE = 'pc-nostr-v69';
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
  else e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));  // media etc.
});
