/* window.nostr — the NIP-07 provider, in the PAGE's world.
 *
 * A content script runs in an isolated world, so anything it defines on `window` is invisible to the
 * page. This file is injected into the page itself and is therefore the ONLY part of the extension a
 * site can touch — which is why it holds no key, no vault and no decision. It marshals a request,
 * hands it to the content script over postMessage, and waits. Every answer that matters is made in
 * the background, which is where the key lives and where the origin is checked against what the
 * user approved.
 *
 * Deliberately minimal and side-effect free: sites detect a signer by the presence of `window.nostr`
 * and some do it at document_start, so this must define the object immediately and never throw.
 *
 * It is a FUNCTION, not a script that runs where it is loaded. content.js stringifies it into an
 * inline <script>, so it never appears in the page as `moz-extension://<uuid>/inject.js` — that UUID
 * is per-install, stable and readable by any page that watches for the node, i.e. a cross-site
 * supercookie that also announces which extension you have. Nothing here may close over anything
 * outside its own body.
 */
var __pcNostrProvider = function () {
  'use strict';
  if (window.nostr) return;                 // another signer got here first; don't fight it

  let seq = 0;
  const pending = new Map();

  window.addEventListener('message', (e) => {
    if (e.source !== window) return;                       // only our own frame
    const d = e.data;
    if (!d || d.__pcnostr !== 'res' || !pending.has(d.id)) return;
    const { resolve, reject, error, method, bytes } = pending.get(d.id);
    pending.delete(d.id);
    if (d.error){ error.message = method + ' failed (' + bytes + ' bytes): ' + d.error; reject(error); }
    else resolve(d.result);
  });

  function call(method, params) {
    const promise = new Promise((resolve, reject) => {
      const id = 'pc' + (++seq) + '.' + Math.random().toString(36).slice(2);
      const value = params && (method === 'nip44.encrypt' ? params.plaintext
                               : method === 'nip44.decrypt' ? params.ciphertext : '');
      let bytes = 0; try{ bytes = new TextEncoder().encode(typeof value === 'string' ? value : '').length; }catch(_){}
      // Create the error at REQUEST time so its stack points to the real page caller. The response
      // fills in operation and size instead of leaving Firefox at this provider's message listener.
      const error = new Error('PosterChan signer ' + method + ' (' + bytes + ' bytes)');
      pending.set(id, { resolve, reject, error, method, bytes });
      window.postMessage({ __pcnostr: 'req', id, method, params }, '*');
      // A request that is never answered — the extension was disabled or the worker died mid-flight
      // — must reject rather than leave the site waiting on a promise forever.
      setTimeout(() => {
        if (!pending.has(id)) return;
        pending.delete(id);
        reject(new Error('PosterChan Passwords did not answer'));
      }, 120000);
    });
    // A page may deliberately fire-and-forget a signer operation. Keep the rejection observable to
    // callers that await it, but mark it handled here as well so a refused/invalid request does not
    // become a global `unhandledrejection` that PosterChan turns into an unrelated action-failed UI.
    promise.catch(() => {});
    return promise;
  }

  function rejected(message) {
    const promise = Promise.reject(new Error(message));
    promise.catch(() => {});
    return promise;
  }

  function nip44Encrypt(pubkey, plaintext) {
    // Do not stringify malformed callers. `undefined` becoming the seven-byte string "undefined"
    // is a valid encryption of the wrong message; objects becoming "[object Object]" is worse.
    const text = typeof plaintext === 'string' ? plaintext : '';
    const size = new TextEncoder().encode(text).length;
    // NIP-44 v2 is one event and has an absolute 1..65535-byte plaintext bound. Chunking here would
    // invent an incompatible protocol. Large media belongs in Blossom with only its pointer/key
    // metadata encrypted in the event.
    if (size < 1 || size > 65535)
      return rejected('NIP-44 plaintext is ' + size + ' bytes; it must be 1..65535. Store large data as an attachment and encrypt only its pointer.');
    return call('nip44.encrypt', { pubkey, plaintext:text });
  }

  function nip44Decrypt(pubkey, ciphertext) {
    const text = typeof ciphertext === 'string' ? ciphertext : '';
    const size = new TextEncoder().encode(text).length;
    if(size < 1) return rejected('NIP-44 ciphertext is empty; refusing a corrupt decrypt request.');
    return call('nip44.decrypt', { pubkey, ciphertext:text });
  }

  const nostr = {
    getPublicKey: () => call('getPublicKey'),
    signEvent: (event) => call('signEvent', { event }),
    getRelays: () => call('getRelays'),
    nip04: {
      encrypt: (pubkey, plaintext) => call('nip04.encrypt', { pubkey, plaintext }),
      decrypt: (pubkey, ciphertext) => call('nip04.decrypt', { pubkey, ciphertext }),
    },
    nip44: {
      encrypt: nip44Encrypt,
      decrypt: nip44Decrypt,
    },
  };

  // Non-writable so a script on the page cannot swap the provider out from under the user after a
  // site has already picked it up.
  try {
    Object.defineProperty(window, 'nostr', { value: nostr, writable: false, configurable: false });
  } catch (_) {
    window.nostr = nostr;
  }
  /* A MARKER IN THE DOM, which is the one thing both worlds can see. content.js runs in the isolated
   * world and cannot read this window's `nostr`, so without this it has no way to know whether the
   * provider actually got installed — only whether the manifest SAYS it should have, which is not the
   * same claim. It falls back to the inline injection when this attribute is missing. */
  try { document.documentElement.setAttribute('data-pc-nostr', '1'); } catch (_) {}
};

/* SELF-RUN IN THE PAGE'S WORLD.
 *
 * Firefox loads this as a content script (isolated world) and content.js stringifies the function
 * into an inline <script> to get it into the page — the long way round, taken because a `src`
 * pointing at the extension publishes a per-install UUID to every page on the web.
 *
 * Chrome has a direct route Firefox did not: a content script registered with `"world": "MAIN"` runs
 * in the page itself. CONFIRMED, not theorised: with the inline-<script> path Brave reported "no
 * NIP-07 extension" on every site, and registering this file in the MAIN world fixed it. Chromium
 * does not let a content script's inline <script> define anything the page can see here; Firefox
 * does, which is why the same code worked there and only there. That is strictly better here — no inline <script> for a site's
 * Content-Security-Policy to refuse, no injected node, and Chrome's extension id is the same for
 * every install, so it is not the supercookie a Firefox UUID would be. build.sh registers this file
 * that way in the generated Chrome manifest, and there is nothing in the page's world to call it, so
 * it calls itself.
 *
 * Distinguishing the two: a content script in an ISOLATED world can see chrome.runtime.id; the
 * page's world cannot. Where it can, content.js is going to do the injecting and this must NOT run
 * (it would define window.nostr in the isolated world, where no site can see it, and then the real
 * injection would find `window.nostr` already set — in the wrong world — and decline).
 */
try {
  if (typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.id) __pcNostrProvider();
} catch (_) {
  try { __pcNostrProvider(); } catch (__) {}
}
