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
    const { resolve, reject } = pending.get(d.id);
    pending.delete(d.id);
    if (d.error) reject(new Error(d.error)); else resolve(d.result);
  });

  function call(method, params) {
    return new Promise((resolve, reject) => {
      const id = 'pc' + (++seq) + '.' + Math.random().toString(36).slice(2);
      pending.set(id, { resolve, reject });
      window.postMessage({ __pcnostr: 'req', id, method, params }, '*');
      // A request that is never answered — the extension was disabled or the worker died mid-flight
      // — must reject rather than leave the site waiting on a promise forever.
      setTimeout(() => {
        if (!pending.has(id)) return;
        pending.delete(id);
        reject(new Error('PosterChan Passwords did not answer'));
      }, 120000);
    });
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
      encrypt: (pubkey, plaintext) => call('nip44.encrypt', { pubkey, plaintext }),
      decrypt: (pubkey, ciphertext) => call('nip44.decrypt', { pubkey, ciphertext }),
    },
  };

  // Non-writable so a script on the page cannot swap the provider out from under the user after a
  // site has already picked it up.
  try {
    Object.defineProperty(window, 'nostr', { value: nostr, writable: false, configurable: false });
  } catch (_) {
    window.nostr = nostr;
  }
};
