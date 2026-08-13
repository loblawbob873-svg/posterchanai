#!/usr/bin/env python3
"""Regression check for the desktop app's BUNDLED client and its standalone ("relays only") mode.

The desktop build ships the web client inside the installer (desktop/build-www.sh) and can run with no
PosterChan instance at all. That mode is not reachable from the web PWA, so `check_client_mobile.py`
never exercises it — every assertion here corresponds to a way it broke, or would have:

  bundled-detection  __PC_API_BASE__ must be DEFINED and EMPTY. app.js keys "am I bundled" off the
                     first and "do I have a server" off the second; conflating them registered the web
                     PWA's /client/sw.js inside a bundle that only has /sw.js (404, no media cache) and
                     removed the instance picker on exactly the installs that needed it.
  nostr-only         PC_NOSTR_ONLY has to be forced on at RUNTIME. The web page bakes it into the
                     template, but one bundle serves every instance, so a baked value is either wrong or
                     permanent.
  gated-nav          Views that need a server (AI, Meme Builder, News, Torrents, 4chan, Server Stats)
                     hidden; relay-only views (Social, Messages, Notes, Passwords, Budget, Games) NOT.
                     A dead button that 404s is worse than an absent one.
  empty-groups       A nav group whose every child was hidden used to leave an empty disclosure triangle.
  settings-reachable Settings is where relays and the instance are set, so it must RENDER without a
                     server. It used to spend ~2.4s failing /api/auth/settings and then show
                     "Couldn't load your settings" — dead-ending the one screen standalone needs.
  relay-prefill      The relay rows come pre-filled with the relays PosterChan actually uses. An empty
                     box asks the user to already know which relays exist.
  save-survives      The single Save button must not throw on the server-only fields it can no longer
                     find. It read #us-email unguarded, which took the relay and media edits down with
                     it — Save silently did nothing at all.
  tor-panel          The native Tor panel renders with a country picker when the shell bridge is present.
  no-console-errors  Any uncaught error at all. "bulk works, single doesn't" is always an exception.
  layout             No horizontal overflow at desktop, tablet and phone widths.

Chrome only — no instance needed, which is the point. Electron itself is NOT driven here: it needs an X
display, and CI/this box have none. app:// scheme registration and the preload bridge are therefore
covered by construction, not by this script; the bridge is STUBBED the way preload injects it.

    venv-unified/bin/python scripts/check_desktop_standalone.py

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / no websockets / build failed).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WWW = os.path.join(ROOT, "desktop", "www")
# Two listeners, and NEITHER may be a fixed number. This check is run by scripts/checkall.py
# alongside twenty others, and it is run ON NODES that are already serving — so a hardcoded port is
# either a collision with a sibling check (9473 was shared by four of them) or with whatever the box
# is really running. The debug port comes from the runner; the HTTP one is bound ephemerally and
# read back, which cannot collide with anything by construction.
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9473)
HTTP_PORT = 0          # 0 = let the kernel pick; the real number is read off the socket below
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-desktop-check"

WIDTHS = [(1280, 860, False), (820, 1180, True), (390, 844, True)]

sys.path.insert(0, ROOT)


def make_nsec():
    """A throwaway key, so the check can log in and reach Settings. Never leaves this process."""
    from app.services.nostr import bech32
    return bech32.encode("nsec", os.urandom(32))


# Stands in for desktop/preload.js. Injected before any page script (addScriptToEvaluateOnNewDocument
# is the CDP equivalent of a preload), because the bundle's shim reads pcShell.instanceSync
# SYNCHRONOUSLY to set __PC_API_BASE__ before the first fetch or WebSocket is constructed.
BRIDGE_STUB = r"""
window.__pcErrors = [];
window.addEventListener('error', e => window.__pcErrors.push(String((e && e.message) || e)));
window.addEventListener('unhandledrejection', e => {
  var r = e && e.reason; var m = String((r && r.message) || r || '');
  // The shim REJECTS root-relative fetches when there is no instance, on purpose, and app.js catches
  // them at the call site. Those are the design working, not errors.
  if (m.indexOf('no instance configured') >= 0) return;
  window.__pcErrors.push('unhandled: ' + m);
});
window.pcShell = {
  instanceSync: '',                        // <- standalone: no server
  getInstance: () => Promise.resolve(''),
  setInstance: () => Promise.resolve(true),
  retry: () => {},
  tor: {
    status: () => Promise.resolve({
      enabled: false, running: false, bootstrapped: false, progress: null, country: '',
      countryName: '', socksPort: 0, error: '', available: true,
      countries: [['us','United States'],['de','Germany'],['jp','Japan']],
    }),
    set: (o) => Promise.resolve(Object.assign({enabled:false, country:'', countries:[['us','United States']]}, o)),
    newCircuit: () => Promise.resolve(true),
    restart: () => Promise.resolve({enabled:false}),
    onStatus: () => {},
  },
};
window.pcClip = { write: () => Promise.resolve(true) };
"""

AUDIT = r"""(() => {
  const q = (s) => document.querySelector(s);
  const nav = (v) => q('.nav-item[data-view="' + v + '"]');
  const hidden = (el) => !el || el.classList.contains('hidden');
  const shown  = (el) => !!el && !el.classList.contains('hidden');
  const out = {};

  out.apiBaseType = typeof window.__PC_API_BASE__;
  out.apiBase     = String(window.__PC_API_BASE__);
  out.nostrOnly   = !!window.PC_NOSTR_ONLY;
  out.bodyStandalone = document.body.classList.contains('standalone');
  out.secure      = !!window.isSecureContext;
  out.subtle      = !!(window.crypto && window.crypto.subtle && window.crypto.subtle.digest);

  // Gated (need a server) vs kept (relays + key only).
  out.gatedShown = ['ai','translate','markets','news','torrents','4chan','stats','meme']
    .filter(v => shown(nav(v)));
  out.keptHidden = ['global','notifications','messages','bookmarks','calls','notes','vault','drafts',
                    'budget','articles','communities','chat','streams','chess','settings']
    .filter(v => hidden(nav(v)));
  out.appsHidden = hidden(q('.rb-apps'));
  out.musicHidden = hidden(q('#nav-music'));

  // A group left with nothing visible inside it is an empty disclosure triangle.
  out.emptyGroups = [...document.querySelectorAll('.nav-group')].filter(g => {
    const kids = [...g.querySelectorAll('.nav-item.sub')];
    return kids.length && kids.every(k => k.classList.contains('hidden')) && !g.classList.contains('hidden');
  }).length;

  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  out.errors = (window.__pcErrors || []).slice(0, 10);
  return out;
})()"""

# The SIGN-IN screen, before any login. Settings has the same controls, but Settings is behind the login —
# so without this, choosing your own relays (or none) meant first signing in to somebody else's instance
# to go and find the switch. On a fresh APK that is the only server anyone has.
AUTH_AUDIT = r"""(() => {
  const q = (s) => document.querySelector(s);
  const out = {};
  out.gateVisible = !!(q('#auth-gate') && !q('#auth-gate').classList.contains('hidden'));
  const row = q('#auth-conn-row');
  out.connRowPresent = !!row;
  out.connRowShown = !!(row && !row.classList.contains('hidden'));
  out.connLabel = (q('#auth-conn-label') || {}).textContent || '';
  // Open the chooser the way a user does, then check every control is really there.
  const btn = q('#btn-auth-conn'); if (btn) btn.click();
  const pane = q('#auth-conn');
  out.paneOpens = !!(pane && !pane.classList.contains('hidden'));
  out.loginHidden = !!(q('#auth-login') && q('#auth-login').classList.contains('hidden'));
  out.hasInstanceInput = !!q('#conn-instance');
  out.hasNoneButton = !!q('#btn-conn-none');
  out.hasRelayBox = !!q('#conn-relays');
  out.relayBoxLines = q('#conn-relays')
    ? String(q('#conn-relays').value || '').split(/\n/).filter(s => s.trim()).length : 0;
  out.relayBoxHasOurs = q('#conn-relays') ? /relay\.poster\.place/.test(q('#conn-relays').value || '') : false;
  // Relays and the server must be INDEPENDENT: setting one may not rewrite or discard the other. This
  // is the shape the first draft got wrong — it offered "use a server" OR "use relays only", which said
  // you had to give up the server to choose your relays.
  out.relaysBeforeServer = (() => {
    try {
      const secs = [...document.querySelectorAll('#auth-conn .auth-sec .auth-sec-t')].map(s => s.textContent || '');
      return /relay/i.test(secs[0] || '');   // relays are the primary setting, the server is the option
    } catch (_) { return false; }
  })();
  out.serverOptional = [...document.querySelectorAll('#auth-conn .auth-sec-t')]
    .some(s => /optional/i.test(s.textContent || ''));

  // Dropping the server with NOTHING saved and an empty box must be refused — relays would then be the
  // only thing left to talk to, and there would be none. Saved relays are cleared first so the check
  // does not depend on what an earlier iteration happened to leave behind.
  if (q('#conn-relays')) {
    const keep = q('#conn-relays').value;
    let saved = null;
    try { saved = localStorage.getItem('pc_nostr_settings');
          const o = JSON.parse(saved || '{}'); delete o.relays; delete o.relaysEnabled;
          localStorage.setItem('pc_nostr_settings', JSON.stringify(o)); } catch (_) {}
    q('#conn-relays').value = '';
    q('#btn-conn-none').click();
    out.emptyRefused = !!((q('#conn-error') || {}).textContent || '').trim();
    try { if (saved !== null) localStorage.setItem('pc_nostr_settings', saved); } catch (_) {}
    q('#conn-relays').value = keep;
    if (q('#conn-error')) q('#conn-error').textContent = '';
  }
  // A junk server address must be refused too, rather than becoming https://typo.
  if (q('#conn-instance')) {
    q('#conn-instance').value = 'not a domain';
    q('#btn-conn-instance').click();
    out.junkRefused = !!((q('#conn-error') || {}).textContent || '').trim();
    q('#conn-instance').value = '';
    if (q('#conn-error')) q('#conn-error').textContent = '';
  }
  const back = q('#btn-conn-back'); if (back) back.click();
  out.backReturns = !!(q('#auth-login') && !q('#auth-login').classList.contains('hidden'));

  // Signup must be possible with no server: the captcha gates admission to a NODE's web-of-trust relay,
  // and with no node there is nothing to be admitted to. Drawing it put an unanswerable box (its image
  // comes from /client/captcha) in front of the button, and signupGo refused to proceed without an answer.
  const su = q('#btn-show-signup'); if (su) su.click();
  out.signupOpens = !!(q('#auth-signup') && !q('#auth-signup').classList.contains('hidden'));
  out.hasGenKey = !!q('#btn-gen-key');

  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  out.errors = (window.__pcErrors || []).slice(0, 10);
  return out;
})()"""

SIGNUP_AUDIT = r"""(() => {
  const q = (s) => document.querySelector(s);
  return {
    keysShown: !!(q('#signup-keys') && !q('#signup-keys').classList.contains('hidden')),
    nsec: (q('#signup-nsec') || {}).textContent || '',
    npub: (q('#signup-npub') || {}).textContent || '',
    captchaDrawn: !!q('#signup-captcha-box'),
    goShown: !!(q('#btn-signup-go') && !q('#btn-signup-go').classList.contains('hidden')),
    errors: (window.__pcErrors || []).slice(0, 10),
  };
})()"""

# The sign-in QR, drawn by the bundle and read back by the bundle's own scanner.
#
# It was server-rendered (POST /client/qr), which is the one dependency this screen cannot have: with
# no instance there is nothing to ask, and the label above it says "scan this". The risks now are both
# packaging ones that a unit test cannot see — qr.js not copied into www/, or the <script> tag lost in
# the shell rewrite — and both would show up here as "no encoder", not as a broken picture.
#
# The decode is jsQR, loaded from the bundle's vendored copy, so this also proves that file is present
# (it was missing from every APK ever built until the vendor tree started being copied whole).
QR_AUDIT = r"""(async () => {
  const out = { encoder: !!window.PCQR, drawn: '', decoded: '', err: '' };
  if (!out.encoder) return out;
  const uri = 'nostrconnect://' + 'a3'.repeat(32) + '?relay=wss%3A%2F%2Frelay.nsec.app&secret='
            + '9f'.repeat(16) + '&name=PosterChan&perms=sign_event%3A1%2Cnip44_encrypt';
  try {
    out.drawn = window.PCQR.dataUrl(uri).slice(0, 24);
    const q = window.PCQR.modules(uri);
    const S = 4, B = 4, dim = (q.size + B * 2) * S;
    const cv = document.createElement('canvas'); cv.width = cv.height = dim;
    const cx = cv.getContext('2d');
    cx.fillStyle = '#fff'; cx.fillRect(0, 0, dim, dim);
    cx.fillStyle = '#000';
    for (let y = 0; y < q.size; y++) for (let x = 0; x < q.size; x++)
      if (q.mod[y][x]) cx.fillRect((x + B) * S, (y + B) * S, S, S);
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = '/static/vendor/qr/jsqr.js'; s.onload = res; s.onerror = () => rej(new Error('no jsqr'));
      document.head.appendChild(s);
    });
    const d = cx.getImageData(0, 0, dim, dim);
    const got = window.jsQR(d.data, dim, dim);
    out.decoded = got ? got.data : '';
    out.want = uri;
  } catch (e) { out.err = String((e && e.message) || e); }
  return out;
})()"""

SETTINGS_AUDIT = r"""(() => {
  const q = (s) => document.querySelector(s);
  const out = {};
  const host = q('#user-settings');
  out.rendered = !!(host && host.querySelector('.us-tabs'));
  out.deadEnd  = !!(host && /Couldn.t load your settings/.test(host.textContent || ''));
  out.tabs = [...document.querySelectorAll('.us-tab')].map(b => b.dataset.tab);

  // Relay pre-fill: the rows exist and name real relays, not a blank box.
  const rows = [...document.querySelectorAll('#set-relay-list input')].map(i => i.value.trim());
  out.relayRows = rows;
  out.relayPrefilled = rows.filter(Boolean).length;
  out.hasPosterPlace = rows.some(u => /relay\.poster\.place/.test(u));
  out.allWss = rows.filter(Boolean).every(u => /^wss?:\/\//.test(u));

  // The switch is meaningless with no built-in relay to fall back to: hidden, and forced on so the one
  // Save path still reads it.
  const sw = q('#set-relays-on');
  out.relaySwitchPresent = !!sw;
  out.relaySwitchChecked = !!(sw && sw.checked);
  out.relaySwitchHidden  = !!(sw && sw.closest('label.fld') && sw.closest('label.fld').classList.contains('hidden'));
  out.relayBodyDisabled  = !!(q('#set-relays-body') && q('#set-relays-body').classList.contains('disabled'));

  // Instance controls must be present (this is the only way back to a server) ...
  out.instanceInput = !!q('#us-instance-inp');
  out.instanceNone  = !!q('#us-instance-none');
  // ... and the server-only fields gone rather than blank-and-savable.
  out.emailField = !!q('#us-email');
  out.newsField  = !!q('#us-news-src');

  // Native Tor panel, from the stubbed bridge.
  out.torRow = !!q('#us-ntor-row');
  out.torCountries = q('#us-ntor-cc') ? q('#us-ntor-cc').options.length : 0;
  out.torAnyFirst = q('#us-ntor-cc') && q('#us-ntor-cc').options.length
    ? q('#us-ntor-cc').options[0].value === '' : false;
  // …and something to click to REACH it. Tor moved out of Profile into its own pane; the controls
  // existing in the DOM says nothing about whether a tab links to them, and an unreachable pane is
  // indistinguishable from a deleted one.
  out.torTab = [...document.querySelectorAll('.us-tabs [data-pane], .us-tabs [data-tab], .us-tab')]
    .some(t => (t.dataset.pane || t.dataset.tab || '') === 'tor'
               || /^\s*tor\s*$/i.test(t.textContent || ''));
  out.torPane = !!q('.us-pane[data-pane="tor"]');

  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  out.errors = (window.__pcErrors || []).slice(0, 10);
  return out;
})()"""


def serve_www():
    global HTTP_PORT
    handler = partial(SimpleHTTPRequestHandler, directory=WWW)
    httpd = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler)
    HTTP_PORT = httpd.server_address[1]          # whatever the kernel just gave us
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def drive(problems):
    import websockets
    page = None
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list"))
            page = [t for t in tabs if t["type"] == "page"][0]
            break
        except Exception:
            await asyncio.sleep(0.5)
    if not page:
        print("SKIP  could not start Chrome")
        return 2

    url = f"http://127.0.0.1:{HTTP_PORT}/index.html"
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        n = [0]

        async def call(method, params=None):
            n[0] += 1
            await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n[0]:
                    if msg.get("error"):
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result")

        async def js(expr):
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                               "awaitPromise": True})
            if r.get("exceptionDetails"):
                return {"__throw": str(r["exceptionDetails"].get("text"))}
            return r["result"].get("value")

        await call("Runtime.enable")
        await call("Page.enable")
        await call("Page.addScriptToEvaluateOnNewDocument", {"source": BRIDGE_STUB})

        for w, h, mobile in WIDTHS:
            label = f"{w}x{h}"
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": w, "height": h, "deviceScaleFactor": 2 if mobile else 1,
                        "mobile": mobile})
            await call("Page.navigate", {"url": url})
            await asyncio.sleep(9)

            res = await js(AUDIT)
            if not isinstance(res, dict) or res.get("__throw"):
                problems.append(f"{label}: audit did not evaluate ({res})")
                continue

            if res["apiBaseType"] != "string":
                problems.append(f"{label}: __PC_API_BASE__ is {res['apiBaseType']}, not a string — "
                                "bundled detection is broken")
            if res["apiBase"] != "":
                problems.append(f"{label}: standalone API base should be empty, got {res['apiBase']!r}")
            if not res["secure"]:
                problems.append(f"{label}: page is not a secure context (crypto.subtle would be gone)")
            if not res["subtle"]:
                problems.append(f"{label}: crypto.subtle missing — the client cannot sign")
            if not res["nostrOnly"]:
                problems.append(f"{label}: PC_NOSTR_ONLY not forced on in standalone")
            if not res["bodyStandalone"]:
                problems.append(f"{label}: body.standalone not set")
            if res["gatedShown"]:
                problems.append(f"{label}: server-only nav still visible: {res['gatedShown']}")
            if res["keptHidden"]:
                problems.append(f"{label}: relay-only nav wrongly hidden: {res['keptHidden']}")
            if not res["appsHidden"]:
                problems.append(f"{label}: 'Get the app' block not hidden (its links need a server)")
            if not res["musicHidden"]:
                problems.append(f"{label}: Music nav not hidden (it needs a server)")
            if res["emptyGroups"]:
                problems.append(f"{label}: {res['emptyGroups']} nav group(s) left visible but empty")
            # Fonts. client.css @font-face's Inter and Orbitron from /static/fonts — root-relative urls
            # INSIDE a stylesheet, which the fetch shim never sees, so a build that forgets to copy them
            # drops the whole app to a system font with no error anywhere. Tested by FETCHING the files,
            # not with document.fonts.check(): font-display:swap loads a face only when a glyph actually
            # needs it, so Orbitron reads "missing" on any screen that happens not to use it yet.
            fonts = await js(
                "Promise.all(['inter','orbitron'].map(f =>"
                " fetch('/static/fonts/'+f+'.woff2').then(r => r.ok ? '' : f+' ('+r.status+')')"
                "  .catch(() => f+' (unreachable)'))).then(a => a.filter(Boolean).join(', '))")
            if fonts:
                problems.append(f"{label}: bundled font missing: {fonts} — "
                                "build-www.sh must copy static/fonts/*.woff2")
            if res["overflow"]:
                problems.append(f"{label}: page scrolls horizontally")
            for e in res["errors"]:
                problems.append(f"{label}: console error: {e}")

        # ---- the sign-in screen, BEFORE any login -------------------------------------------------
        for w, h, mobile in [(1280, 860, False), (390, 844, True)]:
            label = f"sign-in {w}x{h}"
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": w, "height": h, "deviceScaleFactor": 2 if mobile else 1,
                        "mobile": mobile})
            await call("Page.navigate", {"url": url})
            await asyncio.sleep(9)
            # The app boots into GUEST mode showing a public feed, so the sign-in card is not on screen
            # yet — reach it the way a guest does, by clicking the identity chip that offers to log in.
            opened = await js(
                "(() => { const mc=document.querySelector('#me-card');"
                " if(mc && typeof mc.onclick==='function'){ mc.click(); return 'clicked'; }"
                " return 'no guest login affordance'; })()")
            if opened != "clicked":
                problems.append(f"{label}: could not reach the sign-in card from guest mode ({opened})")
                continue
            await asyncio.sleep(2)
            a = await js(AUTH_AUDIT)
            if not isinstance(a, dict) or a.get("__throw"):
                problems.append(f"{label}: sign-in audit did not evaluate ({a})")
                continue
            if not a["gateVisible"]:
                problems.append(f"{label}: the login gate is not showing — cannot audit sign-in")
                continue
            if not a["connRowPresent"]:
                problems.append(f"{label}: no connection row on the sign-in card — the only way to choose "
                                "relays is Settings, which is behind the login")
                continue
            if not a["connRowShown"]:
                problems.append(f"{label}: connection row present but hidden in a bundled build")
            if "no server" not in a["connLabel"].lower():
                problems.append(f"{label}: connection row should say there is no server, says {a['connLabel']!r}")
            if not a["paneOpens"] or not a["loginHidden"]:
                problems.append(f"{label}: the connection chooser does not open")
                continue
            if not a["hasInstanceInput"]:
                problems.append(f"{label}: no instance field in the chooser")
            if not a["hasNoneButton"]:
                problems.append(f"{label}: no 'use no server' button in the chooser")
            if not a["relaysBeforeServer"]:
                problems.append(f"{label}: the server is presented before relays — relays are the primary "
                                "setting and apply with or without a server")
            if not a["serverOptional"]:
                problems.append(f"{label}: the server is not marked optional, so choosing your own relays "
                                "still reads as giving the server up")
            if not a["hasRelayBox"]:
                problems.append(f"{label}: no relay list in the chooser — relays cannot be set pre-login")
            elif a["relayBoxLines"] < 2:
                problems.append(f"{label}: relay list not pre-filled ({a['relayBoxLines']} line(s))")
            elif not a["relayBoxHasOurs"]:
                problems.append(f"{label}: relay pre-fill does not include our own relay")
            if not a.get("emptyRefused"):
                problems.append(f"{label}: 'relays only' accepted an EMPTY relay list — the app would have "
                                "nothing at all to talk to")
            if not a.get("junkRefused"):
                problems.append(f"{label}: a junk server address was accepted instead of refused")
            if not a["backReturns"]:
                problems.append(f"{label}: 'back' does not return to the sign-in options")
            if not a["signupOpens"] or not a["hasGenKey"]:
                problems.append(f"{label}: cannot reach 'create a new identity'")
            if a["overflow"]:
                problems.append(f"{label}: sign-in screen scrolls horizontally")
            for e in a["errors"]:
                problems.append(f"{label}: console error: {e}")

            # Generating a key must work, and must NOT put a server captcha in the way.
            gen = await js("(() => { const b=document.querySelector('#btn-gen-key');"
                           " if(!b) return 'no gen button'; b.click(); return 'clicked'; })()")
            if gen == "clicked":
                await asyncio.sleep(4)
                s2 = await js(SIGNUP_AUDIT)
                if isinstance(s2, dict) and not s2.get("__throw"):
                    if not s2["keysShown"] or not s2["nsec"].startswith("nsec1"):
                        problems.append(f"{label}: key generation did not produce an nsec "
                                        f"({s2.get('nsec','')[:16]!r})")
                    if s2["captchaDrawn"]:
                        problems.append(f"{label}: a server captcha is drawn with no server — its image "
                                        "comes from /client/captcha and signup refuses to proceed without it")
                    if not s2["goShown"]:
                        problems.append(f"{label}: the 'I saved it — enter' button never appeared")
                    for e in s2["errors"]:
                        problems.append(f"{label}: console error after gen: {e}")

            # The signer QR must be drawable with no server, and must actually scan.
            qr = await js(QR_AUDIT)
            if not isinstance(qr, dict) or qr.get("__throw"):
                problems.append(f"{label}: the QR audit did not evaluate ({qr})")
            elif not qr.get("encoder"):
                problems.append(f"{label}: no QR encoder in the bundle — the sign-in screen would tell "
                                "you to scan a QR that cannot be drawn without a server")
            elif qr.get("err"):
                problems.append(f"{label}: drawing or scanning the sign-in QR failed: {qr['err']}")
            elif not str(qr.get("drawn", "")).startswith("data:image/svg"):
                problems.append(f"{label}: the QR is not an inline image ({qr.get('drawn')!r})")
            elif qr.get("decoded") != qr.get("want"):
                problems.append(f"{label}: the sign-in QR does not decode back to its URI "
                                f"({(qr.get('decoded') or 'nothing readable')[:40]!r})")

        # ---- log in and open Settings, at desktop and phone width ----------------------------------
        nsec = make_nsec()
        for w, h, mobile in [(1280, 860, False), (390, 844, True)]:
            label = f"settings {w}x{h}"
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": w, "height": h, "deviceScaleFactor": 2 if mobile else 1,
                        "mobile": mobile})
            await call("Page.navigate", {"url": url})
            await asyncio.sleep(9)
            # The nsec field lives inside a collapsed <details>; a click on a button in a closed
            # details still dispatches, but open it anyway so a failure here is about login and not
            # about visibility.
            ok = await js(
                "(() => { const d=document.querySelector('#auth-key'); if(d) d.open=true;"
                " const i=document.querySelector('#nsec-input');"
                " const b=document.querySelector('#btn-nsec-login');"
                f" if(!i) return 'no #nsec-input'; i.value={json.dumps(nsec)};"
                " if(b) b.click(); else return 'no login button'; return 'clicked'; })()")
            if ok != "clicked":
                problems.append(f"{label}: could not drive nsec login ({ok})")
                continue
            await asyncio.sleep(8)
            nav_ok = await js(
                "(() => { const b=document.querySelector('.nav-item[data-view=\"settings\"]');"
                " if(!b) return 'no settings nav'; b.click(); return 'clicked'; })()")
            if nav_ok != "clicked":
                problems.append(f"{label}: could not open Settings ({nav_ok})")
                continue
            await asyncio.sleep(7)

            s = await js(SETTINGS_AUDIT)
            if not isinstance(s, dict) or s.get("__throw"):
                problems.append(f"{label}: settings audit did not evaluate ({s})")
                continue
            if s["deadEnd"]:
                problems.append(f"{label}: dead-ends on \"Couldn't load your settings\" with no server")
            if not s["rendered"]:
                problems.append(f"{label}: Settings did not render")
                continue
            for t in ("mail", "telegram", "social", "keys"):
                if t in s["tabs"]:
                    problems.append(f"{label}: server-only Settings tab '{t}' still offered")
            for t in ("profile", "relays", "media"):
                if t not in s["tabs"]:
                    problems.append(f"{label}: Settings tab '{t}' missing")
            if s["relayPrefilled"] < 2:
                problems.append(f"{label}: relay rows not pre-filled ({s['relayRows']})")
            if not s["hasPosterPlace"]:
                problems.append(f"{label}: pre-fill does not include our own relay ({s['relayRows']})")
            if not s["allWss"]:
                problems.append(f"{label}: a pre-filled relay is not a ws(s) URL ({s['relayRows']})")
            if not s["relaySwitchPresent"]:
                problems.append(f"{label}: #set-relays-on missing — Save reads it")
            if not s["relaySwitchChecked"]:
                problems.append(f"{label}: relays not forced on in standalone (there is no built-in relay)")
            if not s["relaySwitchHidden"]:
                problems.append(f"{label}: the relay switch should be hidden in standalone (not a choice)")
            if s["relayBodyDisabled"]:
                problems.append(f"{label}: the relay editor is greyed out in standalone")
            if not s["instanceInput"] or not s["instanceNone"]:
                problems.append(f"{label}: instance controls missing — no way back to a server")
            if s["emailField"] or s["newsField"]:
                problems.append(f"{label}: server-only fields still shown (they save nowhere)")
            if not s["torRow"]:
                problems.append(f"{label}: native Tor panel missing with the shell bridge present")
            elif s["torCountries"] < 2:
                problems.append(f"{label}: Tor country picker has {s['torCountries']} option(s)")
            elif not s["torAnyFirst"]:
                problems.append(f"{label}: 'Any country' is not the first Tor option")
            if s.get("torRow") and not s.get("torPane"):
                problems.append(f"{label}: the Tor controls are not in the Tor pane")
            if s.get("torRow") and not s.get("torTab"):
                problems.append(f"{label}: no Tor tab in Settings — the pane exists but nothing links "
                                "to it, which is the same as it not being there")
            if s["overflow"]:
                problems.append(f"{label}: Settings scrolls horizontally")
            for e in s["errors"]:
                problems.append(f"{label}: console error: {e}")

            # Save must not throw on the fields it can no longer find — that took the relay edits with it.
            await js("window.__pcErrors=[]")   # only what the Save itself raises
            saved = await js("(() => { const b=document.querySelector('#us-save');"
                             " if(!b) return 'no save button'; b.click(); return 'clicked'; })()")
            if saved != "clicked":
                problems.append(f"{label}: could not find the Save button ({saved})")
            else:
                # Read the errors BEFORE the reload. Enabling the relay list is a change, so Save sets
                # needReload and reloads after 600ms — correct behaviour (the relay sockets have to be
                # re-opened), but it resets every JS global, so __pcErrors has to be collected first.
                await asyncio.sleep(0.4)
                for e in json.loads(await js("JSON.stringify((window.__pcErrors||[]).slice(0,6))") or "[]"):
                    problems.append(f"{label}: Save raised: {e}")
                # localStorage survives the reload, but reading DURING it hits a dying execution context
                # and looks exactly like "nothing was saved" — so poll instead of taking one shot at a
                # guessed moment. The real regression this guards: Save writes the client-side settings
                # AFTER building the server body, so a throw on a missing server-only field leaves the
                # key absent entirely.
                stored, raw = None, None
                for _ in range(12):
                    await asyncio.sleep(2)
                    stored = await js("JSON.stringify((JSON.parse("
                                      "localStorage.getItem('pc_nostr_settings')||'{}')).relays||null)")
                    if isinstance(stored, str) and stored not in ("null",):
                        break
                if not isinstance(stored, str) or stored == "null":
                    raw = await js("String(localStorage.getItem('pc_nostr_settings'))")
                    problems.append(f"{label}: Save did not persist the relay list "
                                    f"(it threw before reaching the client-side saves). "
                                    f"pc_nostr_settings={raw!r}")
                elif "relay.poster.place" not in stored:
                    problems.append(f"{label}: saved relay list lost the pre-filled relays: {stored}")
    return 1 if problems else 0


async def run():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2

    # ALWAYS rebuild. This used to build only when www/ was absent, which meant a bundle left over from
    # an earlier session was tested instead of the working tree — every assertion silently about old
    # code, reported green. It cost a real one: a new client file that build-www.sh had never copied
    # (and a <script> tag the shell rewrite had never seen) passed the packaging checks here because
    # neither the file nor the tag was in the stale www/ being served. The build takes about a second.
    print("building desktop/www …")
    r = subprocess.run(["bash", "build-www.sh"], cwd=os.path.join(ROOT, "desktop"),
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("SKIP  desktop/build-www.sh failed:\n" + (r.stderr or r.stdout))
        return 2
    if not shutil.which("google-chrome-stable"):
        print("SKIP  google-chrome-stable not found")
        return 2

    httpd = serve_www()
    # /tmp is a tmpfs on this box, so a ~130 MB Chrome profile left behind is 130 MB of RAM held until
    # the next reboot. Start clean, and take it away afterwards.
    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        return await drive(problems)
    finally:
        httpd.shutdown()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)
        if problems:
            print(f"\n{len(problems)} problem(s):")
            for p in problems:
                print("  - " + p)
        else:
            print("\ndesktop standalone: clean")


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
