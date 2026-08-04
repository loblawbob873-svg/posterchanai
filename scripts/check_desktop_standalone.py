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
CDP_PORT = 9473
HTTP_PORT = 9474
PROFILE = "/tmp/pc-desktop-check"
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

  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  out.errors = (window.__pcErrors || []).slice(0, 10);
  return out;
})()"""


def serve_www():
    handler = partial(SimpleHTTPRequestHandler, directory=WWW)
    httpd = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler)
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

    if not os.path.isdir(WWW) or not os.path.isfile(os.path.join(WWW, "index.html")):
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
