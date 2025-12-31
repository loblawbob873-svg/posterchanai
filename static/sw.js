// Service Worker for Posterchanai PWA
const CACHE_NAME = 'posterchanai-v1';
const urlsToCache = [
  '/',
  '/static/css/style.css',
  '/static/favicon.png',
  '/static/manifest.json'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(urlsToCache))
  );
});

self.addEventListener('fetch', event => {
  event.respondWith(
    caches.match(event.request)
      .then(response => response || fetch(event.request))
  );
});
