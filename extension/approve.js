/* The approval prompt for a NIP-07 request.
 *
 * A real extension window on purpose. A page can draw a convincing copy of any in-page dialog,
 * including one that says "PosterChan wants to sign" — it cannot draw a browser window, and the
 * user can see this is not part of the site.
 *
 * It says WHAT is being asked, not just that something is. "Sign this event?" with no detail is a
 * rubber stamp; the kind is what separates "post a note" from "replace your contact list" or "send a
 * zap", and it is the reason approval is remembered per kind rather than per site.
 */
'use strict';

const B = (typeof browser !== 'undefined') ? browser : chrome;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* The kinds worth naming. An unknown kind is shown as its number rather than described, because
 * inventing a friendly label for something we do not recognise is how a user approves the wrong
 * thing — "kind 30078" is honest, "Application data" would be a guess. */
const KINDS = {
  0: 'update your profile',
  1: 'publish a note',
  3: 'REPLACE YOUR CONTACT LIST (who you follow)',
  4: 'send a legacy direct message',
  5: 'DELETE one of your events',
  6: 'repost a note',
  7: 'react to a note',
  1059: 'send a private message',
  9734: 'REQUEST A PAYMENT (zap) from your wallet',
  10002: 'replace your relay list',
  22242: 'sign in to a relay',
  30023: 'publish a long-form article',
};
// Kinds where an approval that is remembered is worth a second thought.
const HEAVY = new Set([3, 5, 9734, 10002, 0]);

const METHODS = {
  getPublicKey: 'see your public key (your npub)',
  getRelays: 'see which relays you use',
  'nip04.encrypt': 'encrypt a message with your key',
  'nip04.decrypt': 'DECRYPT a message with your key',
  'nip44.encrypt': 'encrypt a message with your key',
  'nip44.decrypt': 'DECRYPT a message with your key',
};

(async () => {
  const id = location.hash.slice(1);
  let res = null;
  try { res = await B.runtime.sendMessage({ type: 'approve-ask', id }); } catch (_) {}
  if (!res || !res.ok) {
    $('#what').textContent = 'That request has expired.';
    $('#allow').disabled = true;
    $('#deny').textContent = 'Close';
    $('#deny').onclick = () => window.close();
    return;
  }
  const req = res.req;
  $('#origin').textContent = req.origin;

  if (req.method === 'signEvent') {
    /* The kind is a number the background already validated — but it is read here as an object
     * index and as a Set member, and those two disagree about types. `KINDS["3"]` finds the contact
     * list; `HEAVY.has("3")` does not, so the label would shout REPLACE YOUR CONTACT LIST while the
     * warning stayed silent and Remember stayed ticked. One coercion, used for both. */
    const kind = Number.isInteger(req.kind) ? req.kind : -1;
    const label = (Object.prototype.hasOwnProperty.call(KINDS, kind) && KINDS[kind]) ||
                  ('sign an event of kind ' + kind);
    $('#what').innerHTML = 'It wants to <span class="kind">' + esc(label) + '</span>.' +
      (req.preview ? '<pre>' + esc(req.preview) + '</pre>' : '');
    if (HEAVY.has(kind)) {
      $('#warn').textContent = 'This one changes something durable. Only allow it if you asked the ' +
                               'site to do it just now.';
      $('#remember').checked = false;    // don't let a heavy action be waved through by default
    }
  } else {
    const m = Object.prototype.hasOwnProperty.call(METHODS, req.method) ? METHODS[req.method] : null;
    $('#what').innerHTML = 'It wants to <span class="kind">' + esc(m || req.method) + '</span>.';
    if (/decrypt/.test(req.method))
      $('#warn').textContent = 'Allowing this lets the site read messages sent to you.';
  }

  const answer = async (allow) => {
    try { await B.runtime.sendMessage({ type: 'approve-answer', id, allow,
                                        remember: $('#remember').checked }); } catch (_) {}
    window.close();
  };
  $('#allow').onclick = () => answer(true);
  $('#deny').onclick = () => answer(false);
  // Closing the window IS a refusal: the background treats an unanswered prompt as a deny, so
  // nothing is signed by walking away.
})();
