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

/* WHICH APP IS THIS REQUEST FOR? Every mini app on this instance shares ONE origin, so they share
 * ONE service worker — and this file used to answer that question with "the first client whose path
 * looks like a loader". With two games open that is a coin toss: opening Quake III served Half-Life,
 * because Half-Life's loader was still alive and came first out of `matchAll()`. It is not merely
 * cosmetic either — it is one app's bytes delivered into another app's frame.
 *
 * So every instance carries a TOKEN, minted per session by the client, and a request is answered
 * only by the loader holding the same one. It rides in the query rather than the fragment because a
 * fragment never reaches a worker: `request.url` is serialised without it, and a NAVIGATION is
 * exactly the case with nothing else to go on (`clientId` is empty for one, by design). The token is
 * an unguessable uuid for the same reason — an app can read its own URL, and a guessable token would
 * let it ask for somebody else's files.
 *
 * A request that cannot be attributed is REFUSED. Guessing is the bug. */
const TOKEN = '__xdc';
const CLIENT_TOKENS = new Map();      // client id -> token, for after an app rewrites its own URL

function tokenOf(url) {
  try { return new URL(url).searchParams.get(TOKEN) || ''; } catch (_) { return ''; }
}
function isLoader(url) {
  try { return new URL(url).pathname.indexOf('/__sandbox__/') === 0; } catch (_) { return false; }
}
function remember(id, tok) {
  if (!id || !tok) return;
  CLIENT_TOKENS.set(id, tok);
  // A worker outlives many games. Bounded so a long session cannot grow this without limit.
  if (CLIENT_TOKENS.size > 200) CLIENT_TOKENS.delete(CLIENT_TOKENS.keys().next().value);
}

/* The loader that owns this request, or a refusal.
 *
 * Four ways to learn the token, in order of certainty, because an app may navigate itself and may
 * rewrite its own URL (Quake III edits its query string on boot):
 *   1. this client id, remembered from its navigation — survives history.replaceState;
 *   2. the request URL — how a NAVIGATION carries it, since it has no client yet;
 *   3. the requesting client's own URL;
 *   4. the referrer — an in-app link to another page, which inherits no query of its own.
 * With no token at all and exactly ONE loader open there is no ambiguity, so that is answered: it is
 * also what keeps a client too old to mint a token working. Two loaders and no token is a guess, and
 * a guess is what this exists to stop. */
async function loaderFor(event, request) {
  const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const loaders = all.filter((c) => isLoader(c.url));

  let tok = (event.clientId && CLIENT_TOKENS.get(event.clientId)) || '';
  if (!tok) tok = tokenOf(request.url);
  if (!tok && event.clientId) {
    let me = null;
    try { me = await self.clients.get(event.clientId); } catch (_) { me = null; }
    if (me) tok = tokenOf(me.url);
  }
  if (!tok && request.referrer) tok = tokenOf(request.referrer);

  if (tok) {
    remember(event.resultingClientId || event.clientId, tok);
    const hit = loaders.find((c) => tokenOf(c.url) === tok);
    if (hit) return hit;
    // A token nobody answers to: that app's window was closed. Say so — falling through to another
    // loader is precisely how one game ends up serving another one's files.
    throw new Error('that mini app is not open any more');
  }
  if (loaders.length === 1) return loaders[0];
  if (!loaders.length) throw new Error('sandbox loader is gone');
  throw new Error('could not tell which mini app this request belongs to');
}
/* 90s. The parent has to inflate the entry before it can answer, and a mini app's largest file can
 * be tens of megabytes — Half-Life's campaign archives are 29-75 MB each. A 15s ceiling turned a slow
 * phone into "the app took too long to answer", which reads as a broken app rather than a big one. */
const RPC_TIMEOUT = 90000;

self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });

/* Ask the loader document for one file. A MessageChannel per request rather than a long-lived port:
 * there is nothing to re-establish when the app navigates and replaces its document, and a reply can
 * never be delivered to the wrong request. */
async function askClient(event, request) {
  /* The LOADER is the client to ask, and it is one of the documents at /__sandbox__/ — WHICH one is
   * the whole of loaderFor() above. The app's own frame is a client too, and asking it would be a
   * loop: it cannot answer, because everything it knows came from here. On a navigation there is no
   * app client yet at all, which is the other half of why the loader is a separate document that
   * stays alive. */
  const loader = await loaderFor(event, request);

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
      const res = await askClient(event, req);
      const headers = new Headers();
      for (const k in (res.headers || {})) headers.set(k, res.headers[k]);
      /* BYTES, not base64, whenever the parent sends them. A mini app can be very large — the
       * published Half-Life is 178 MB and holds three campaign archives of 29-75 MB each — and
       * base64 costs a third more on the wire plus a string encode and decode of that size at every
       * hop. The parent transfers the ArrayBuffer instead, which is a pointer move rather than a
       * copy. `body` stays supported so a stale cached loader still works. */
      const body = res.bytes ? new Uint8Array(res.bytes) : (res.body ? unb64(res.body) : null);
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
