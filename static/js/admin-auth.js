/* Authentication for the admin panel, without a cookie.
 *
 * The panel is a page from the server, framed by the Nostr client. That made it depend on a COOKIE:
 * an iframe's document load carries cookies and nothing else, so there was no way to hand it the
 * bearer token the client already holds. In a bundled app (desktop `app://posterchan`, the APK) that
 * cookie is CROSS-SITE, so it needs SameSite=None — which browsers only accept with Secure, which is
 * only sent over HTTPS. Against a .onion, which is plain HTTP by design, no cookie can ever be sent,
 * so the panel could not work there at all: /admin saw no session, redirected to the client, and the
 * app rendered the website where the panel should be.
 *
 * So the page stops relying on the cookie. The client hands it the token over postMessage — no secret
 * in the URL, so nothing lands in history, a Referer, or a server log — and every /api/ call made by
 * this page carries it as `Authorization: Bearer`. That is scheme-agnostic: it works over https, over
 * a .onion, over a LAN box, and from any bundled origin.
 *
 * WHY A fetch WRAPPER and not 40 edited call sites: admin.js, admin-bots.js and admin-emoji.js make
 * roughly forty requests, some through csrfFetch and some through bare fetch(). Editing each is a
 * change you can be one call site wrong about, silently, in a panel where being wrong means an
 * unexplained 401. One wrapper cannot miss one.
 *
 * Loaded FIRST, before any admin script, so the wrapper exists before the first request.
 */
(function () {
  'use strict';

  var token = '';
  var settle;
  // Resolves when we know how this page will authenticate: a token arrived, or nothing did and we
  // fall back to whatever cookie the browser has (the ordinary same-origin case, unchanged).
  var ready = new Promise(function (res) { settle = res; });
  var done = false;
  function finish() { if (!done) { done = true; settle(); } }

  // Nothing framed us → there is no token coming; go with cookies immediately.
  if (window.parent === window) finish();

  window.addEventListener('message', function (e) {
    // Only our embedder may hand us a credential. The token is being GIVEN to us rather than read
    // from us, so the worst a stray sender achieves is a 401 — but there is no reason to accept one.
    if (e.source !== window.parent) return;
    var d = e.data;
    if (!d || d.type !== 'pc-admin-token') return;
    token = String(d.token || '');
    finish();
  });

  // Ask for it. Carries no secret, so '*' is safe here; the reply goes to our exact origin because the
  // client knows the URL it framed.
  try { window.parent.postMessage({ type: 'pc-admin-hello' }, '*'); } catch (_) { finish(); }

  // Never hang the panel on a client that does not answer (an older build, or a plain browser visit
  // with the page opened in a tab). Cookies still work there; this is only the token path giving up.
  setTimeout(finish, 2500);

  var _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = '';
    try { url = (typeof input === 'string') ? input : (input && input.url) || ''; } catch (_) {}
    var api = url.indexOf('/api/') === 0 ||
              (url.indexOf('://') > 0 && url.indexOf(location.origin + '/api/') === 0);
    if (!api) return _fetch(input, init);
    return ready.then(function () {
      if (!token) return _fetch(input, init);
      // A Request object carries its own headers, and passing `init.headers` alongside one REPLACES
      // them wholesale — which would silently drop a Content-Type and turn a working POST into a 422.
      // Nothing here uses Request today; this exists so that adding one later cannot break auth.
      if (input && typeof input !== 'string' && input.headers) {
        var hdrs = new Headers(input.headers);
        if (!hdrs.get('Authorization')) hdrs.set('Authorization', 'Bearer ' + token);
        return _fetch(new Request(input, { headers: hdrs }), init);
      }
      // Copy rather than mutate: callers reuse an options object, and quietly writing a credential
      // into theirs would send it on whatever they use it for next.
      init = Object.assign({}, init || {});
      // NEVER clobber an Authorization the caller set — an upload may carry its own NIP-98 header.
      var h = init.headers;
      if (h && typeof h.get === 'function') {
        var copy = new Headers(h);
        if (!copy.get('Authorization')) copy.set('Authorization', 'Bearer ' + token);
        init.headers = copy;
      } else {
        h = Object.assign({}, h || {});
        var has = false;
        for (var k in h) { if (String(k).toLowerCase() === 'authorization') has = true; }
        if (!has) h['Authorization'] = 'Bearer ' + token;
        init.headers = h;
      }
      return _fetch(input, init);
    });
  };

  // The page is served to anyone now — it holds no data, only fields — so it has to say when the
  // credentials it ended up with are not an admin's. Previously the SERVER refused to render it, which
  // it can no longer do: the token that authorises the page arrives after the page has loaded.
  window.__pcAdminAuth = ready.then(function () {
    // window.fetch, NOT the captured original: the gate has to be asked with the SAME credentials
    // everything else uses, or it reports "not an admin" while every real call is perfectly authorised.
    return window.fetch('/api/admin/settings', { credentials: 'include' }).then(function (r) {
      if (r.ok) return true;
      var main = document.querySelector('.admin-container') || document.body;
      main.innerHTML = '<div style="max-width:44ch;margin:12vh auto;text-align:center;line-height:1.6">'
        + '<h2>Admin sign-in needed</h2>'
        + '<p class="muted">This panel is open, but the credentials this page has are not an '
        + 'administrator\'s.</p><p><a href="/client?next=%2Fadmin">Sign in with your admin Nostr key</a></p>'
        + '</div>';
      return false;
    }).catch(function () { return false; });
  });
})();
