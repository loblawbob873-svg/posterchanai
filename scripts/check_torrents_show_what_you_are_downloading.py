#!/usr/bin/env python3
"""THE TORRENTS SCREEN MUST SHOW THE TORRENTS, AND ITS CONTROLS MUST STILL WORK TWO SECONDS LATER.

    venv-unified/bin/python scripts/check_torrents_show_what_you_are_downloading.py
    venv-unified/bin/python scripts/check_torrents_show_what_you_are_downloading.py --simulate <bug>

WHY THIS EXISTS. Torrents was the last big screen in the client with NO browser check at all — the
only coverage was `tests/test_torrents.py` over the server side, which cannot see a view. That is
exactly the gap every regression of the last month fell through, and they were all one shape:

    A SURFACE THAT RENDERS PERFECTLY WHILE SHOWING NONE OF THE USER'S DATA.

The Blossom picker drew an empty grid for folders holding 79 files. The wallpaper picker said "No
pictures yet." about a drive with pictures in it. The mail folder list did not exist. In each case
nothing threw, nothing logged, and every layout check stayed green, because a screen that paints an
empty state IS a screen that painted.

Torrents has four ways to land in that state and one extra way of its own:

  1. THE LIST IS DROPPED. The server answers N torrents and the box prints "Nothing downloading."
  2. A FAILURE IS DRESSED AS AN ANSWER. `/list` 503s because this node has no torrent client, and
     the screen says "Nothing downloading." — which is a lie about a setting, and sends the user
     looking for their torrents instead of to Admin → Tools. 401/403 and a plain 500 are three more
     different facts and must read as three different sentences, never as "you have none".
  3. THE NOSTR TAB IGNORES WHAT THE RELAY SENT. Same rule, other source (NIP-35 kind 2003).
  4. THE TAB STRIP IS DEAD. Both tabs draw, neither switches — the shape where `bind()` stops
     matching its own markup and every button below it silently stops being wired.
  5. AND THE ONE THAT IS SPECIFIC TO THIS VIEW: **the second repaint.** This list refreshes itself
     every 2s and deliberately repaints IN PLACE while the list is unchanged in shape, so a click
     target does not move under a finger. That fast path is a second, near-identical renderer, and
     it is the one nobody looks at: if it blanks the box, the screen is correct for two seconds and
     empty forever after; if it forgets `_torBindRows`, Pause and Remove are dead two seconds after
     you arrive, with the buttons still drawn. Both are invisible to any check that renders once.

Plus the leak the code's own comment promises is impossible: the 2s poll is self-cancelling, so
leaving the view must leave NO live timer behind.

HOW IT RUNS. No instance, no network, no keys, no torrent client. The whole torrents region is
LIFTED OUT OF THE SHIPPED app.js as one contiguous slice (never a copy — a rename fails this check
rather than quietly making it test something else) and run in a real headless Chrome against a stub
`/api/torrent` and a stub relay. A real browser, because the fast path is built out of `dataset`,
`CSS.escape` and `replaceWith`, none of which a string harness exercises honestly.

PROVING IT CAN FAIL. `--simulate` puts each bug back, from this harness, without touching a shipped
file:

  --simulate drop-list             the list renders the empty state whatever the server said   (1)
  --simulate empty-on-error        every /list failure reads as "Nothing downloading."         (2)
  --simulate nostr-empty           the Nostr tab throws away the events it just fetched        (3)
  --simulate unbound-tabs          the tab strip draws and neither tab switches                (4)
  --simulate blank-repaint         the 2s in-place repaint empties the box                     (5)
  --simulate unbound-after-repaint the 2s repaint leaves Pause/Remove unbound                  (5)
  --simulate leaky-poll            leaving the view leaves the 2s poll running                 (6)

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / no websockets).
"""
import argparse
import asyncio
import functools
import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")

CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9494)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-torrents-check"
VIEWPORT = (1280, 900)

SIMULATIONS = ("drop-list", "empty-on-error", "nostr-empty", "unbound-tabs",
               "blank-repaint", "unbound-after-repaint", "leaky-poll")


# --------------------------------------------------------------------------------------------
# Lifting the shipped code. Never a copy of it.
# --------------------------------------------------------------------------------------------
def lift_re(src, pattern, what):
    m = re.search(pattern, src, re.S)
    if not m:
        raise SystemExit("FAIL  could not lift %s out of app.js — it was renamed or reshaped, so "
                         "this check would be testing a copy of it instead of the real thing." % what)
    return m.group(0)


def swap(js, old, new, what):
    """A simulation must PATCH the real text. An anchor that stopped matching would make the
    simulation a no-op, i.e. a check that reports 'the bug cannot be reproduced' about a bug it
    simply failed to inject — the exact way these harnesses go vacuous."""
    if old not in js:
        raise SystemExit("FAIL  simulation anchor for %s no longer matches app.js" % what)
    return js.replace(old, new, 1)


def lifted_app_js(sim):
    # The client source, split: the torrents screen moved into discover.js (a factory app.js builds
    # on first use), while the card the search list also draws (torrentCard, _magnet) and
    # _fmtBytes stayed in app.js. Lifted from both, still never a copy.
    sys.path.insert(0, ROOT)
    from tests.client_source import client_source, state_shims
    src = client_source()

    # The NIP-35 publisher, the downloads manager and both tabs: one contiguous region of
    # discover.js, from the section's banner to the end of the Nostr tab's renderer.
    js = lift_re(src,
                 r"\n  // ---------- torrents \(NIP-35, kind 2003\) ----------.*?"
                 r"\n  async function _renderTorrentsNostr\(.*?\n  \}\n",
                 "the torrents region")
    # The cards, which stayed in app.js because the search results draw them too.
    js += lift_re(src, r"\n  function _magnet\(.*?\n  \}", "_magnet()")
    js += lift_re(src, r"\n  function torrentCard\(.*?\n  \}", "torrentCard()") + "\n"
    # _fmtBytes lives above it and every row and card calls it.
    js = lift_re(src, r"\n  function _fmtBytes\(.*?\n  \}", "_fmtBytes()") + "\n" + js

    if sim == "drop-list":
        js = swap(js, "    const list=(j&&j.torrents)||[];",
                      "    const list=[];", sim)
    elif sim == "empty-on-error":
        js = swap(js,
                  "    catch(err){\n      if(S.VIEW!=='torrents') return;",
                  "    catch(err){\n      if(S.VIEW!=='torrents') return;\n"
                  "      box.innerHTML='<div class=\"empty\">Nothing downloading.</div>'; return;", sim)
    elif sim == "nostr-empty":
        js = swap(js, "    const tors=evs.sort((a,b)=>b.created_at-a.created_at);",
                      "    const tors=[];", sim)
    elif sim == "unbound-tabs":
        js = swap(js, "const bind=()=>{ $$('.tor-tabs .ntab',feed).forEach(",
                      "const bind=()=>{ [].forEach(", sim)
    elif sim == "blank-repaint":
        js = swap(js, "    if(!force && box.dataset.sig===sig){",
                      "    if(!force && box.dataset.sig===sig){ box.innerHTML='';", sim)
    elif sim == "unbound-after-repaint":
        js = swap(js, "      _torBindRows(box);\n      return;\n    }",
                      "      return;\n    }", sim)
    elif sim == "leaky-poll":
        js = swap(js,
                  "      if(S.VIEW!=='torrents' || _torTab!=='dl' || "
                  "!document.getElementById('tm-list')){ _torStopPoll(); return; }",
                  "      if(false){ _torStopPoll(); return; }", sim)
    elif sim:
        raise SystemExit("FAIL  unknown simulation %r" % sim)
    # discover.js reads app.js's live bindings (VIEW, GUEST, _aiToken) through `S`; this page's own
    # `let VIEW = …` stubs are those bindings.
    return js + "\n" + state_shims(js) + "\n"


# --------------------------------------------------------------------------------------------
# The page. Everything the region reaches for, and nothing it does not.
# --------------------------------------------------------------------------------------------
PRELUDE = r"""
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.prototype.slice.call((r||document).querySelectorAll(s));
const enc = s => (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LOGO = '/logo.png';
let VIEW = 'torrents', GUEST = false, _aiToken = 'tok';

// --- what the stub server holds, and every question the page asked it -----------------------
window.__srv = { status:200, torrents:[], calls:[] };
window.__toasts = [];
window.__live = new Set();          // timers this page has running RIGHT NOW

const _si = window.setInterval, _ci = window.clearInterval;
window.setInterval = function(fn, ms){ const id=_si.apply(window, arguments); window.__live.add(id); return id; };
window.clearInterval = function(id){ window.__live.delete(id); return _ci.call(window, id); };

window.fetch = async (url, opts) => {
  const u = String(url); window.__srv.calls.push(u);
  const st = window.__srv.status;
  if (st !== 200) return { ok:false, status:st, json: async () => ({ detail:'stub refused' }) };
  if (/\/api\/torrent\/list/.test(u))
    return { ok:true, status:200, json: async () => ({ torrents: window.__srv.torrents }) };
  return { ok:true, status:200, json: async () => ({ ok:true }) };
};

const ensureAiSession = async () => {};
const toast = m => { window.__toasts.push(String(m)); };
const uiConfirm = async () => true;
const uiPrompt = async () => '';
const modal = () => {}, closeModal = () => {};
const publish = async () => ({ ok:true });
const renderView = () => {};
const _guestPrompt = () => {};
const _isDesktopApp = () => false;
const copyValue = () => {};
const renderProfileView = () => {};
const decorateProfiles = () => {};
const needProfile = () => {};
const profOf = () => ({ name:'anon', picture:LOGO });
const Store = { saveEvent(){} };
window.__relayEvents = [];
const Relay = { query: async () => window.__relayEvents };
"""

PAGE = """<!doctype html><meta charset="utf-8"><title>torrents check</title>
<body><div id="feed"></div>
<script>
%s
%s
window.__T = { renderTorrents, _torRefresh, _torRow, get tab(){ return _torTab; },
               set view(v){ VIEW = v; }, get view(){ return VIEW; } };
window.__ready = true;
</script>
"""


def page(sim):
    js = lifted_app_js(sim).replace("</", "<\\/")
    return PAGE % (PRELUDE.replace("</", "<\\/"), js)


def serve(html):
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            raw = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=ROOT))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


SEED = [
    dict(num=0, info_hash="a" * 40, name="gentoo-amd64-20260913.iso",
         progress=0.42, state="downloading", size=4042217472, downloaded=1697731334,
         download_rate=1200000, upload_rate=64000, seeders=31, peers=9,
         is_paused=False, is_finished=False),
    dict(num=1, info_hash="b" * 40, name="Big Buck Bunny (2008) 1080p",
         progress=1.0, state="seeding", size=355856562, downloaded=355856562,
         download_rate=0, upload_rate=250000, seeders=4, peers=2,
         is_paused=False, is_finished=True),
    dict(num=2, info_hash="c" * 40, name="slackware64-15.0-install-dvd",
         progress=0.07, state="downloading", size=3800000000, downloaded=266000000,
         download_rate=0, upload_rate=0, seeders=0, peers=0,
         is_paused=True, is_finished=False),
]

NOSTR_EVENTS = [
    dict(id="e1", pubkey="f" * 64, created_at=1700000300, kind=2003,
         content="A Linux ISO somebody published.",
         tags=[["title", "debian-13.1.0-amd64-netinst"], ["x", "d" * 40],
               ["file", "debian.iso", "660000000"], ["t", "linux"]]),
    dict(id="e2", pubkey="e" * 64, created_at=1700000200, kind=2003,
         content="", tags=[["title", "Sintel (2010) 4K"], ["x", "1" * 40],
                           ["file", "sintel.mkv", "12000000000"]]),
    dict(id="e3", pubkey="d" * 64, created_at=1700000100, kind=2003,
         content="", tags=[["title", "Public domain music pack"], ["x", "2" * 40]]),
]


async def drive(port, sim):
    import websockets
    td = PROFILE if os.environ.get("PC_CHECK_PROFILE") else tempfile.mkdtemp(prefix="pc-torrents-")
    shutil.rmtree(td, ignore_errors=True)
    os.makedirs(td, exist_ok=True)
    chrome = next((shutil.which(x) for x in
                   ("google-chrome-stable", "chromium", "google-chrome", "chromium-browser")
                   if shutil.which(x)), None)
    if not chrome:
        print("SKIP  no Chrome on this box")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--remote-debugging-port=%d" % CDP_PORT, "--user-data-dir=%s" % td, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tab = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json/list" % CDP_PORT))
                tab = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:
                await asyncio.sleep(0.25)
        if not tab:
            print("SKIP  Chrome never opened a debuggable tab")
            return 2

        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=1 << 25) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        if msg.get("error"):
                            raise RuntimeError("%s: %s" % (method, msg["error"]))
                        return msg.get("result") or {}

            async def js(expr):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if r.get("exceptionDetails"):
                    d = (r["exceptionDetails"].get("exception") or {}).get("description")
                    return {"__throw": str(d or r["exceptionDetails"].get("text"))}
                return (r.get("result") or {}).get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": VIEWPORT[0], "height": VIEWPORT[1],
                        "deviceScaleFactor": 1, "mobile": False})
            await call("Page.navigate", {"url": "http://127.0.0.1:%d/" % port})
            for _ in range(80):
                if await js("!!window.__ready") is True:
                    break
                await asyncio.sleep(0.1)
            else:
                print("FAIL  the lifted torrents code never evaluated — it threw at parse time")
                return 1

            bad = []

            def check(ok, rule, detail):
                if not ok:
                    bad.append((rule, detail))

            seed = json.dumps(SEED)
            evs = json.dumps(NOSTR_EVENTS)

            # ---- 1. the list the server reports is the list on screen -----------------------
            r = await js("""(async()=>{
              __srv.status=200; __srv.torrents=%s; __T.view='torrents';
              await __T.renderTorrents();
              const rows=[...document.querySelectorAll('#tm-list .tm-item')];
              return { rows: rows.length,
                       names: rows.map(r=>(r.querySelector('.tm-name')||{}).textContent||''),
                       pct: rows.map(r=>((r.querySelector('.tm-meta span')||{}).textContent||'')),
                       empty: /Nothing downloading/.test(document.getElementById('tm-list').textContent) };
            })()""" % seed)
            if isinstance(r, dict) and r.get("__throw"):
                print("FAIL  rendering the downloads tab threw: %s" % r["__throw"])
                return 1
            check(r["rows"] == 3, "downloads-render",
                  "the server reported 3 torrents and the screen drew %d rows" % r["rows"])
            check(not r["empty"], "downloads-render",
                  "the screen printed \"Nothing downloading.\" about 3 torrents it was handed")
            check(any("gentoo" in x for x in r["names"]), "downloads-render",
                  "no row carried the torrent's name: %r" % (r["names"],))
            check(any("42" in x for x in r["pct"]), "downloads-render",
                  "no row carried its progress: %r" % (r["pct"],))

            # ---- 2. a failure is a fact about the server, never "you have none" -------------
            for status, want, what in ((503, "no torrent client", "no client on this node"),
                                       (403, "access", "not allowed to use it"),
                                       (500, "reach", "the client could not be reached")):
                r = await js("""(async()=>{
                  __srv.status=%d; __T.view='torrents';
                  await __T.renderTorrents();
                  const t=document.getElementById('tm-list').textContent;
                  return { txt:t, empty:/Nothing downloading/.test(t) };
                })()""" % status)
                check(not r["empty"], "error-is-not-empty",
                      "/list %d (%s) reads on screen as \"Nothing downloading.\"" % (status, what))
                check(want.lower() in r["txt"].lower(), "error-is-not-empty",
                      "/list %d does not say %s — it says %r" % (status, what, r["txt"][:90]))

            # ---- 3. the Nostr tab shows what the relay sent ---------------------------------
            r = await js("""(async()=>{
              __srv.status=200; __relayEvents=%s; __T.view='torrents';
              await __T.renderTorrents();               // back to dl first, then switch
              const nt=[...document.querySelectorAll('.tor-tabs .ntab')].find(b=>b.dataset.tt==='nostr');
              if(!nt) return { noTab:true };
              nt.click(); await new Promise(r=>setTimeout(r,120));
              const cards=[...document.querySelectorAll('.tor-card')];
              return { cards: cards.length, tab: __T.tab,
                       titles: cards.map(c=>(c.querySelector('.tor-title')||{}).textContent||''),
                       gets: document.querySelectorAll('.tor-get').length,
                       empty: /No torrents found on the relay/.test(document.getElementById('feed').textContent) };
            })()""" % evs)
            if r.get("noTab"):
                check(False, "tabs-are-bound", "the view drew no Nostr tab button at all")
            else:
                check(r["tab"] == "nostr", "tabs-are-bound",
                      "clicking the Nostr tab left the view on %r — the tab strip is not wired" % r["tab"])
                check(r["cards"] == 3, "nostr-tab-renders",
                      "the relay answered 3 kind-2003 events and the tab drew %d cards" % r["cards"])
                check(not r["empty"], "nostr-tab-renders",
                      "the Nostr tab printed \"No torrents found on the relay yet\" about 3 it was handed")
                check(r["gets"] == 3, "nostr-tab-renders",
                      "%d of 3 cards offered \"Download here\"" % r["gets"])

            # ---- 4. and the strip switches BACK, with the rows rebuilt ----------------------
            r = await js("""(async()=>{
              const dt=[...document.querySelectorAll('.tor-tabs .ntab')].find(b=>b.dataset.tt==='dl');
              if(!dt) return { noTab:true };
              dt.click(); await new Promise(r=>setTimeout(r,200));
              return { tab: __T.tab, rows: document.querySelectorAll('#tm-list .tm-item').length };
            })()""")
            if r.get("noTab"):
                check(False, "tabs-are-bound", "the Nostr tab drew no way back to Downloads")
            else:
                check(r["tab"] == "dl", "tabs-are-bound",
                      "clicking Downloads left the view on %r" % r["tab"])
                check(r["rows"] == 3, "tabs-are-bound",
                      "coming back to Downloads drew %d rows, not 3" % r["rows"])

            # ---- 5. THE SECOND REPAINT. The one nobody looks at. ---------------------------
            r = await js("""(async()=>{
              __srv.status=200; __srv.torrents=%s; __T.view='torrents';
              await __T.renderTorrents();
              const before=document.querySelectorAll('#tm-list .tm-item').length;
              // Same torrents, moved on: this is exactly the 2s tick, and it takes the in-place path.
              __srv.torrents = __srv.torrents.map(t=>({...t, progress: Math.min(1, t.progress+0.25),
                                                              downloaded: t.downloaded+1000}));
              await __T._torRefresh(false);
              const rows=[...document.querySelectorAll('#tm-list .tm-item')];
              const first=rows[0]||null;
              return { before, after: rows.length,
                       pct: first ? (first.querySelector('.tm-meta span')||{}).textContent : '',
                       bound: !!(first && first.querySelector('.tm-toggle') &&
                                 typeof first.querySelector('.tm-toggle').onclick === 'function'),
                       delBound: !!(first && first.querySelector('.tm-del') &&
                                 typeof first.querySelector('.tm-del').onclick === 'function'),
                       empty: /Nothing downloading/.test(document.getElementById('tm-list').textContent) };
            })()""" % seed)
            check(r["after"] == 3 and not r["empty"], "repaint-keeps-the-list",
                  "the 2s in-place repaint left %d rows (empty-state: %s) — the screen is correct "
                  "for two seconds and blank after that" % (r["after"], r["empty"]))
            check("67" in r["pct"], "repaint-keeps-the-list",
                  "the repaint did not move the progress on (first row reads %r)" % r["pct"])
            check(r["bound"] and r["delBound"], "repaint-rebinds-the-buttons",
                  "after the 2s repaint Pause/Remove are drawn but unbound — the buttons are dead "
                  "two seconds after you open the view")

            # ---- 6. leaving the view leaves nothing running --------------------------------
            r = await js("""(async()=>{
              __srv.status=200; __srv.torrents=%s; __T.view='torrents';
              await __T.renderTorrents();
              const during = __live.size;
              __T.view='home';                         // the user went somewhere else
              await new Promise(r=>setTimeout(r,2400)); // one full tick of the 2s poll
              return { during, after: __live.size };
            })()""" % seed)
            check(r["during"] >= 1, "poll-stops-when-you-leave",
                  "the downloads tab started no refresh timer at all, so progress never moves")
            check(r["after"] == 0, "poll-stops-when-you-leave",
                  "%d timer(s) still running after leaving the view — the poll outlived the screen "
                  "and keeps the session busy for the rest of the visit" % r["after"])

            if bad:
                print("FAIL  %d rule(s) broken" % len(bad))
                for rule, detail in bad:
                    print("  [%s] %s" % (rule, detail))
                return 1
            print("OK  the torrents screen shows what the server and the relay reported, its tabs "
                  "switch, its 2s repaint keeps the rows and their buttons, and leaving it stops "
                  "the poll")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", choices=SIMULATIONS,
                    help="put a known bug back, to prove this check can fail")
    a = ap.parse_args()

    if importlib.util.find_spec("websockets") is None:
        print("SKIP  websockets not installed")
        return 2
    html = page(a.simulate)
    srv, port = serve(html)
    try:
        rc = asyncio.run(drive(port, a.simulate))
    finally:
        srv.shutdown()
    if a.simulate:
        if rc == 1:
            print("simulation %r was caught, as it must be" % a.simulate)
            return 0
        if rc == 2:
            return 2
        print("VACUOUS  simulation %r passed — this check does not actually test that rule"
              % a.simulate)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
