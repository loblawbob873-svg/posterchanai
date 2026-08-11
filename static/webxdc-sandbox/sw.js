/* The webxdc sandbox's service worker: it serves a mini app whose files are NOT on this server.
 *
 * Every request the app makes is answered from the .xdc archive held by the PosterChan tab that
 * opened this frame — the worker asks the loader document (see index.html), which forwards to that
 * tab over postMessage and hands the bytes back. Nothing is fetched from the network, ever, which is
 * the webxdc spec's one hard requirement ("MUST deny all forms of Internet access") and the reason
 * an app posted by a stranger can be run at all.
 *
 * A REQUEST THAT CANNOT BE ANSWERED IS REFUSED, NOT PASSED THROUGH. The fetch handler covers every
 * request from this origin including cross-origin ones, and anything that is not a file in the
 * archive gets a 403 — not `fetch(event.request)`. Falling back to the network is the obvious
 * defensive default and it is precisely the hole this whole design exists to close: one line of
 * `catch(() => fetch(req))` and a game can phone home with whatever it has read.
 */
'use strict';

const RESERVED = /^\/(sw\.js$|__sandbox__\/)/;      // the loader and this file — never the app's
const RPC_TIMEOUT = 15000;

self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });

/* Ask the loader document for one file. A MessageChannel per request rather than a long-lived port:
 * there is nothing to re-establish when the app navigates and replaces its document, and a reply can
 * never be delivered to the wrong request. */
async function askClient(request) {
  const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  /* The LOADER is the client to ask, and it is the one at /__sandbox__/. The app's own frame is a
   * client too, and asking it would be a loop: it cannot answer, because everything it knows came
   * from here. On a navigation there is no app client yet at all, which is the other half of why the
   * loader is a separate document that stays alive. */
  const loader = all.find((c) => { try { return new URL(c.url).pathname.indexOf('/__sandbox__/') === 0; } catch (_) { return false; } });
  if (!loader) throw new Error('sandbox loader is gone');

  let body = null;
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    const buf = await request.arrayBuffer();
    body = buf.byteLength ? b64(new Uint8Array(buf)) : null;
  }
  const headers = {};
  for (const [k, v] of request.headers) headers[k.toLowerCase()] = v;

  const ch = new MessageChannel();
  const answer = new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('the app took too long to answer')), RPC_TIMEOUT);
    ch.port1.onmessage = (ev) => {
      clearTimeout(t);
      const d = ev.data;
      if (d && d.ok) resolve(d.res);
      else reject(new Error((d && d.message) || 'refused'));
    };
  });
  loader.postMessage({ t: 'fetch', request: { url: request.url, method: request.method, headers, body } }, [ch.port2]);
  return answer;
}

function b64(bytes) {
  let s = '';
  // Chunked: String.fromCharCode.apply blows the argument limit somewhere around a hundred thousand
  // bytes, and an app's own bundle is comfortably past that.
  for (let i = 0; i < bytes.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(s);
}
function unb64(s) {
  const bin = atob(s || '');
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function refuse(reason, code) {
  return new Response(reason, {
    status: code || 403,
    headers: { 'content-type': 'text/plain; charset=utf-8' },
  });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  let url;
  try { url = new URL(req.url); } catch (_) { event.respondWith(refuse('bad url', 400)); return; }

  // Anything not on this origin is the app trying to reach the outside world. Refused here as well
  // as by the CSP the parent sets, because two independent answers to "can this app phone home" is
  // the right number for the property the whole feature rests on.
  if (url.origin !== self.location.origin) {
    event.respondWith(refuse('webxdc apps have no network access', 403));
    return;
  }
  if (RESERVED.test(url.pathname)) return;            // the loader and this worker: let them through

  event.respondWith((async () => {
    try {
      const res = await askClient(req);
      const headers = new Headers();
      for (const k in (res.headers || {})) headers.set(k, res.headers[k]);
      const body = res.body ? unb64(res.body) : null;
      // 204/304 must not carry a body — constructing a Response with one throws, which would surface
      // as a broken app rather than as an empty response.
      const empty = res.status === 204 || res.status === 304;
      return new Response(empty ? null : body, {
        status: res.status || 200,
        statusText: res.statusText || '',
        headers,
      });
    } catch (e) {
      return refuse('sandbox: ' + ((e && e.message) || 'failed'), 502);
    }
  })());
});
