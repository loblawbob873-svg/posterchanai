/* THE ADMIN PANEL'S OWN ALERT / CONFIRM / PROMPT.
 *
 * "Admin - Relay -> run auto-prune now button causes desktop to split in half."
 *
 * That button called the native `confirm()`. In the desktop shell that is not an in-page dialog at
 * all — Electron opens a REAL window for it, and sway, which tiles, puts that window beside the
 * shell and gives it half the screen. On the web it merely blocks the renderer; in the APK's WebView
 * it can be suppressed entirely, so the branch behind it never runs and the button silently does
 * nothing.
 *
 * The client learned this long ago and has `uiConfirm`/`uiPrompt`. The ADMIN panel never did, and
 * it is a separate page (loaded in a full-height iframe), so it could not reach them: sixty-seven
 * native dialogs across admin.js, admin-bots.js, admin-emoji.js and the relay tab. This file is the
 * one they can all use.
 *
 * `pcConfirm`/`pcPrompt` return PROMISES, because a DOM dialog cannot answer synchronously the way
 * `window.confirm` does — so every caller had to be converted rather than aliased. `pcAlert` is a
 * drop-in: it returns nothing and nobody reads the result.
 *
 * Self-contained styling. This has to work on the one screen an operator reaches for when something
 * else is already broken, so it does not depend on a stylesheet having loaded or on a class defined
 * somewhere else.
 */
(function (root) {
  'use strict';

  var OPEN = null;   // one at a time — a stack of dialogs in a corner is how you lose track of what
                     // you agreed to, and the second Escape then answers the wrong question.

  function el(tag, css, text) {
    var e = document.createElement(tag);
    if (css) e.setAttribute('style', css);
    if (text != null) e.textContent = text;
    return e;
  }

  function sheet(message, kind, def) {
    return new Promise(function (resolve) {
      if (OPEN) { try { OPEN(); } catch (e) {} }
      var bg = el('div',
        'position:fixed;inset:0;z-index:2147483000;background:rgba(6,4,16,.72);' +
        'display:flex;align-items:center;justify-content:center;padding:20px;box-sizing:border-box');
      var box = el('div',
        'background:#161226;color:#f2eefc;border:1px solid #3a2f5e;border-radius:14px;' +
        'box-shadow:0 18px 60px rgba(0,0,0,.55);max-width:520px;width:100%;padding:20px;' +
        'font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif');
      var msg = el('div', 'white-space:pre-wrap;margin-bottom:16px', String(message == null ? '' : message));
      box.appendChild(msg);

      var input = null;
      if (kind === 'prompt') {
        input = el('input',
          'width:100%;box-sizing:border-box;padding:9px 11px;margin-bottom:16px;border-radius:9px;' +
          'border:1px solid #3a2f5e;background:#0f0b1c;color:#f2eefc;font:inherit');
        input.value = def == null ? '' : String(def);
        box.appendChild(input);
      }

      var row = el('div', 'display:flex;gap:10px;justify-content:flex-end');
      var BTN = 'padding:9px 16px;border-radius:9px;border:1px solid #3a2f5e;background:#221a3a;' +
                'color:#f2eefc;font:inherit;cursor:pointer';
      var done = function (value) {
        if (OPEN !== close) return;
        close();
        resolve(value);
      };
      var close = function () {
        OPEN = null;
        document.removeEventListener('keydown', onKey, true);
        try { bg.remove(); } catch (e) {}
      };
      var onKey = function (e) {
        if (e.key === 'Escape') { e.preventDefault(); done(kind === 'alert' ? undefined : (kind === 'prompt' ? null : false)); }
        else if (e.key === 'Enter' && kind !== 'alert') {
          e.preventDefault(); done(kind === 'prompt' ? input.value : true);
        }
      };

      if (kind !== 'alert') {
        var cancel = el('button', BTN, 'Cancel');
        cancel.type = 'button';
        cancel.onclick = function () { done(kind === 'prompt' ? null : false); };
        row.appendChild(cancel);
      }
      var ok = el('button', BTN + ';border-color:#7b5cff;background:#4b2ecc', kind === 'alert' ? 'OK' : 'Continue');
      ok.type = 'button';
      ok.onclick = function () { done(kind === 'alert' ? undefined : (kind === 'prompt' ? input.value : true)); };
      row.appendChild(ok);
      box.appendChild(row);
      bg.appendChild(box);
      /* Clicking the backdrop is a cancel, never a confirm — the opposite mistake is destructive. */
      bg.addEventListener('click', function (e) {
        if (e.target === bg) done(kind === 'alert' ? undefined : (kind === 'prompt' ? null : false));
      });
      (document.body || document.documentElement).appendChild(bg);
      OPEN = close;
      document.addEventListener('keydown', onKey, true);
      try { (input || ok).focus(); } catch (e) {}
    });
  }

  root.pcAlert = function (message) { return sheet(message, 'alert'); };
  root.pcConfirm = function (message) { return sheet(message, 'confirm'); };
  root.pcPrompt = function (message, def) { return sheet(message, 'prompt', def); };
})(window);
