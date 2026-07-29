// Service Worker for Poster-chan AI PWA
const CACHE_NAME = 'posterchanai-v59';
const STATIC_ASSETS = [
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

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames
          .filter(name => name !== CACHE_NAME)
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

  // Skip API calls, WebSocket, and navigation - let browser handle normally
  if (url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/ws') ||
      event.request.mode === 'navigate') {
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
