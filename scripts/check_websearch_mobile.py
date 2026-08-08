#!/usr/bin/env python3
"""Layout + behaviour check for WEB SEARCH, at phone and desktop widths.

Run BEFORE deploying a Web Search change:

    venv-unified/bin/python scripts/check_websearch_mobile.py

check_client_mobile.py only ever loads the timeline — it never opens this screen. This drives the
real websearch.js against a stubbed `window.__PC` (no server, no relay, no login) with canned
results, and audits what a phone actually gets.

Assertions, each a way THIS screen breaks:

  horizontal-overflow   A result carries a raw URL and a title from an arbitrary site. Long unbroken
                        strings are the classic reason a search page scrolls sideways on a phone.
  ios-zoom-trap         The query box under 16px: iOS Safari zooms the page on focus and never zooms
                        back out — on the one input this whole screen exists for.
  tiny-tap-target       A filter chip or an action button under 32px tall. There are four actions per
                        result card, so they are thumb targets or they are nothing.
  results-under-nav     Scrolled to the END of the results, the last one is still under the fixed
                        .mobilenav — i.e. the list reserves no room for it and its last entry can
                        never be read or tapped. (Mid-list cards below the fold are just a list;
                        this measures the bottom, which is the part a phone actually loses.)
  state-lost            THE one the feature is judged on: leaving the view and coming back must NOT
                        lose the query, the results, or where you were in them. #feed is shared by
                        every view and app.js blanks it on entry, so a screen that keeps its results
                        in the DOM has already lost them.
  reader-dead-end       Opening a result must show the PAGE (an iframe of it, which is what "open
                        this result" means) with a way back to the results — a phone has no second
                        pane and no browser chrome to fall back on. The text-only Reader mode must
                        still be one tap away, and the frame must be a definite size: an <iframe>
                        with no height collapses to 150px and the page scrolls in a letterbox.
  reader-scroll-lost    …and coming back must land where the results were, not at the top of a list
                        you had already worked through.
  missing-control       The search box, the filters, or a result's actions did not render at all.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / websockets).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Two phone widths, plus a desktop width — the results list is one column everywhere, but the sticky
# search bar and the chip row behave differently once the sidebar takes its share.
WIDTHS = [(390, 844, True), (360, 780, True), (1280, 860, False)]
PORT = 9477
PROFILE = "/tmp/pc-websearch-mobile-check"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<!-- #feed is the app's ONE scroll container (.feed = flex:1; overflow-y:auto; padding-bottom:90px,
     which is what reserves room for the fixed nav). Reproduced here, because a plain <div> lets the
     WINDOW scroll instead — under which the module's scroll save/restore reads 0 forever and both
     the "keeps your place" tests pass or fail for reasons that have nothing to do with the code. -->
<div class="app" style="display:flex;flex-direction:column;height:100dvh">
  <div id="feed" class="feed"></div>
</div>
<nav class="mobilenav glass"><button class="nav-item"><b>Home</b></button></nav>
<div id="modal-root"></div><div id="toast-root"></div>
<script src="/static/js/client/sprite.js"></script>
<script>
// ---- stub host -------------------------------------------------------------------
// The module takes every helper off window.__PC and every result off /api/websearch/*, so a stubbed
// fetch is enough — and is the point: this tests the screen, not the node.
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.__view = 'websearch';
window.__toasts = [];
// A long, unbroken URL and title — the shape that pushes a page sideways.
const LONG = 'https://example.com/some/extremely/long/path/that/never/breaks/anywhere-at-all/'
           + 'because-real-urls-do-not-contain-spaces-1234567890';
function results(n, page){
  return Array.from({length:n}, (_,i)=>({
    title: (i===0 ? 'A headline with an unbreakable token '+('x'.repeat(60)) : 'Result '+(page||1)+'.'+(i+1)),
    url: (i===0 ? LONG : 'https://site'+i+'.example.com/article/'+i),
    content: 'A snippet of the page, long enough to wrap onto more than one line on a phone. '.repeat(2),
    engine: 'google', published: '2026-01-0'+((i%9)+1), thumbnail: '', img_src: '', length: '',
  }));
}
window.fetch = async (url, opts) => {
  const u = String(url);
  const j = d => ({ ok:true, status:200, json: async()=>d });
  if(u.startsWith('/api/websearch/search')){
    const p = new URLSearchParams(u.split('?')[1]||'');
    window.__searches = (window.__searches||0) + 1;
    return j({ results: results(8, +p.get('page')||1), answers: [], suggestions: ['related one','related two'], error: null });
  }
  if(u.startsWith('/api/websearch/read'))
    return j({ url:LONG, title:'The article', content: Array.from({length:40},(_,i)=>'Paragraph '+i+' of the article body.').join('\n'), error:null });
  if(u.startsWith('/api/websearch/summarize')) return j({ url:LONG, title:'The article', summary:'A summary.' });
  if(u.startsWith('/api/websearch/overview'))
    return j({ query:'q', overview:'An overview citing [1] and [2].', sources:[{n:1,title:'One',url:'https://a.example'},{n:2,title:'Two',url:'https://b.example'}] });
  return j({});
};
window.__PC = {
  $, $$, enc,
  toast: m => { window.__toasts.push(m); },
  compose: o => { window.__composed = o; },
  modal: (html, onMount) => { const bg=document.createElement('div'); bg.className='modal-bg';
    bg.innerHTML = '<div class="modal glass neon-border">'+html+'</div>';
    $('#modal-root').appendChild(bg); if(onMount) onMount(bg.querySelector('.modal')); },
  closeModal: () => { const m=$('#modal-root .modal-bg'); if(m) m.remove(); },
  authFetch: (u,o) => window.fetch(u,o),
  ensureAiSession: async () => ({ can_ai:true }),
  switchView: v => { window.__view = v; },
  get ME(){ return {pubkey:'me'}; },
  get VIEW(){ return window.__view; },
};
window.PCNotes = { save: async (n)=>{ window.__saved = n; return { id:'n1', queued:false }; } };
</script>
<script src="/static/js/client/websearch.js"></script>
<script>
(async function(){
  for(let i=0;i<80 && !window.PCWebSearch;i++) await new Promise(r=>setTimeout(r,50));
  window.PCWebSearch.render();
  window.__ready = true;
})();
</script>
</body></html>"""

AUDIT = r"""(() => {
  const out = {overflow:false, small:[], zoomy:[], cards:0, bar:false, chips:0,
               acts:0, reader:false, back:false, listBottom:0, navTop:0, q:'', ov:false};
  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  const vis = el => el && (!el.checkVisibility || el.checkVisibility());
  const box = el => el.getBoundingClientRect();

  const inp = document.querySelector('#ws-q');
  out.bar = !!vis(inp);
  if (inp) {
    out.q = inp.value;
    const fs = parseFloat(getComputedStyle(inp).fontSize);
    if (fs < 16) out.zoomy.push({cls:'#ws-q', fs});
  }
  out.chips = document.querySelectorAll('.ws-chip[data-cat]').length;
  out.cards = document.querySelectorAll('.ws-card').length;
  out.acts = document.querySelectorAll('.ws-card .ws-acts .btn').length;
  out.ov = !!document.querySelector('#ws-ov-slot');
  out.reader = !!document.querySelector('.ws-reader');
  out.back = !!document.querySelector('#ws-back');

  document.querySelectorAll('.ws-chip, .ws-acts .btn, .ws-go, #ws-back, #ws-more').forEach(b => {
    if (!vis(b)) return;
    const r = box(b);
    if (r.height < 32) out.small.push({sel: b.className, text: (b.textContent||'').trim().slice(0,20), h: Math.round(r.height)});
  });
  const sel = document.querySelector('#ws-time');
  if (sel) { const fs = parseFloat(getComputedStyle(sel).fontSize); if (fs < 16 && window.innerWidth <= 820) out.zoomy.push({cls:'#ws-time', fs}); }

  const last = document.querySelector('.ws-card:last-of-type') || document.querySelector('.ws-rtext');
  const nav = document.querySelector('.mobilenav');
  out.listBottom = last ? box(last).bottom : 0;
  out.navTop = nav && vis(nav) ? box(nav).top : window.innerHeight;
  out.scrollTop = (document.getElementById('feed')||{}).scrollTop || 0;
  return out;
})()"""

SEARCH = r"""(async () => {
  const inp = document.querySelector('#ws-q');
  if (!inp) return {error:'no query box'};
  inp.value = 'test query';
  document.querySelector('#ws-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
  for (let i=0;i<60 && !document.querySelector('.ws-card'); i++) await new Promise(r=>setTimeout(r,50));
  return {cards: document.querySelectorAll('.ws-card').length};
})()"""

# Leave the view the way app.js does — it blanks #feed and calls render() again on return.
LEAVE_AND_RETURN = r"""(async () => {
  const feed = document.getElementById('feed');
  feed.scrollTop = 400;
  const wasScroll = feed.scrollTop;
  await new Promise(r=>setTimeout(r,150));            // let the scroll listener record it
  window.__view = 'home';
  feed.innerHTML = '<div class="spinner"></div>';     // exactly what renderView(true) does
  await new Promise(r=>setTimeout(r,120));
  window.__view = 'websearch';
  const before = window.__searches;
  window.PCWebSearch.render();
  for (let i=0;i<40 && !document.querySelector('.ws-card'); i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,250));            // the scroll restore is a rAF
  return { q: (document.querySelector('#ws-q')||{}).value || '',
           cards: document.querySelectorAll('.ws-card').length,
           scroll: feed.scrollTop, wasScroll,
           refetched: window.__searches > before };
})()"""

# The card itself opens the result now — there is no separate "Read here" button to click.
OPEN_READER = r"""(async () => {
  const feed = document.getElementById('feed');
  feed.scrollTop = 350;
  await new Promise(r=>setTimeout(r,150));
  const card = document.querySelector('.ws-card');
  if (!card) return {error:'no result card to open'};
  card.click();
  for (let i=0;i<60 && !document.querySelector('.ws-frame'); i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,250));
  const fr = document.querySelector('.ws-frame');
  const nav = document.querySelector('.mobilenav');
  const vis = el => el && (!el.checkVisibility || el.checkVisibility());
  return { reader: !!document.querySelector('.ws-reader'),
           back: !!document.querySelector('#ws-back'),
           frame: !!fr,
           frameH: fr ? Math.round(fr.getBoundingClientRect().height) : 0,
           frameBottom: fr ? Math.round(fr.getBoundingClientRect().bottom) : 0,
           navTop: (nav && vis(nav)) ? Math.round(nav.getBoundingClientRect().top) : window.innerHeight,
           mode: !!document.querySelector('#ws-mode'),
           original: !!document.querySelector('.ws-rbar a[target="_blank"]'),
           readerOpen: !!(window.PCWebSearch.readerOpen && window.PCWebSearch.readerOpen()) };
})()"""

# …and the text mode has to still be reachable, since a page the frame renders badly is exactly when
# you want it.
TOGGLE_READER = r"""(async () => {
  const b = document.querySelector('#ws-mode');
  if (!b) return {error:'no Page/Reader toggle'};
  b.click();
  for (let i=0;i<60 && !document.querySelector('.ws-rtext p'); i++) await new Promise(r=>setTimeout(r,50));
  return { paras: document.querySelectorAll('.ws-rtext p').length,
           frame: !!document.querySelector('.ws-frame') };
})()"""

BACK_TO_RESULTS = r"""(async () => {
  const feed = document.getElementById('feed');
  const b = document.querySelector('#ws-back');
  if (!b) return {error:'no back control'};
  b.click();
  for (let i=0;i<40 && !document.querySelector('.ws-card'); i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,250));
  return { cards: document.querySelectorAll('.ws-card').length, scroll: feed.scrollTop,
           readerOpen: !!(window.PCWebSearch.readerOpen && window.PCWebSearch.readerOpen()) };
})()"""

# Scroll to the very end of the results and measure the LAST one against the nav: a list is allowed
# to run past the fold, it is not allowed to end underneath a fixed bar.
BOTTOM_OF_LIST = r"""(async () => {
  const feed = document.getElementById('feed');
  feed.scrollTop = feed.scrollHeight;
  await new Promise(r=>setTimeout(r,250));
  const cards = document.querySelectorAll('.ws-card');
  const last = cards[cards.length-1];
  const nav = document.querySelector('.mobilenav');
  const vis = el => el && (!el.checkVisibility || el.checkVisibility());
  return { bottom: last ? last.getBoundingClientRect().bottom : 0,
           navTop: (nav && vis(nav)) ? nav.getBoundingClientRect().top : window.innerHeight };
})()"""

SAVE_AND_SHARE = r"""(async () => {
  const n = document.querySelector('.ws-card .ws-note'), s = document.querySelector('.ws-card .ws-share');
  if (!n || !s) return {error:'the result has no Notes/Share action'};
  n.click(); await new Promise(r=>setTimeout(r,250));
  s.click(); await new Promise(r=>setTimeout(r,80));
  return { saved: !!window.__saved, savedBody: (window.__saved||{}).body || '',
           shared: !!window.__composed, sharedText: (window.__composed||{}).text || '' };
})()"""


async def drive(url):
    import websockets
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2

        problems = []
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG js error:", json.dumps(r["exceptionDetails"])[:400])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for w, h, phone in WIDTHS:
                label = f"{w}px"
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2 if phone else 1, "mobile": phone})
                await call("Emulation.setTouchEmulationEnabled",
                           {"enabled": phone, "maxTouchPoints": 5 if phone else 0})
                await call("Page.navigate", {"url": url})
                ready = False
                for _ in range(60):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        ready = True
                        break
                if not ready:
                    print(f"SKIP  {label}: the page never finished rendering Web Search")
                    return 2

                r = await js(AUDIT)
                if r is None:
                    print(f"SKIP  {label}: page did not evaluate")
                    return 2
                if not r["bar"]:
                    problems.append((label, "missing-control", "the query box did not render"))
                    continue
                if not r["chips"]:
                    problems.append((label, "missing-control", "no category filters rendered"))
                if r["overflow"]:
                    problems.append((label, "horizontal-overflow", "the empty screen scrolls sideways"))

                s = await js(SEARCH, awaited=True)
                if not s or s.get("error") or not s.get("cards"):
                    problems.append((label, "missing-control", f"the search rendered no results ({(s or {}).get('error')})"))
                    continue

                r = await js(AUDIT)
                if r["overflow"]:
                    problems.append((label, "horizontal-overflow",
                                     "the results scroll the page sideways — a long URL or title is not wrapping"))
                if not r["acts"]:
                    problems.append((label, "missing-control", "a result has no actions (Share / Notes / Summarize)"))
                if not r["ov"]:
                    problems.append((label, "missing-control", "the AI-overview slot is missing"))
                if phone:
                    for z in r["zoomy"]:
                        problems.append((label, "ios-zoom-trap",
                                         f"{z['cls']} is {z['fs']}px — iOS zooms the page on focus"))
                    for t in r["small"]:
                        problems.append((label, "tiny-tap-target",
                                         f"{t['text'] or t['sel']} is {t['h']}px tall"))
                    bot = await js(BOTTOM_OF_LIST, awaited=True)
                    if bot and bot["bottom"] > bot["navTop"] + 1:
                        problems.append((label, "results-under-nav",
                                         f"scrolled to the end, the last result's bottom ({round(bot['bottom'])}px) "
                                         f"is under the nav ({round(bot['navTop'])}px)"))

                # THE feature promise: leave and come back with everything intact.
                st = await js(LEAVE_AND_RETURN, awaited=True)
                if not st:
                    problems.append((label, "state-lost", "could not run the leave-and-return test"))
                else:
                    if st["q"] != "test query":
                        problems.append((label, "state-lost", f"the query box came back {st['q']!r}"))
                    if not st["cards"]:
                        problems.append((label, "state-lost", "the results were gone on return"))
                    if st["refetched"]:
                        problems.append((label, "state-lost",
                                         "coming back re-ran the search — the results must be kept, not refetched"))
                    if st["wasScroll"] and abs(st["scroll"] - st["wasScroll"]) > 40:
                        problems.append((label, "state-lost",
                                         f"came back at {round(st['scroll'])}px instead of {round(st['wasScroll'])}px"))

                # A result opens IN the app, and comes back to where you were.
                rd = await js(OPEN_READER, awaited=True)
                if not rd or rd.get("error"):
                    problems.append((label, "reader-dead-end", f"could not open a result ({(rd or {}).get('error')})"))
                else:
                    if not rd["reader"] or not rd["frame"]:
                        problems.append((label, "reader-dead-end", "opening a result showed no page"))
                    if rd["frame"] and rd["frameH"] < 200:
                        problems.append((label, "reader-dead-end",
                                         f"the page frame is only {rd['frameH']}px tall — an <iframe> with no "
                                         "height collapses to 150px and the page scrolls in a letterbox"))
                    if phone and rd["frame"] and rd["frameBottom"] > rd["navTop"] + 1:
                        problems.append((label, "results-under-nav",
                                         f"the page frame's bottom ({rd['frameBottom']}px) is under the "
                                         f"nav ({rd['navTop']}px)"))
                    if not rd["mode"]:
                        problems.append((label, "reader-dead-end", "no Page/Reader toggle"))
                    else:
                        tg = await js(TOGGLE_READER, awaited=True)
                        if not tg or tg.get("error") or not tg.get("paras"):
                            problems.append((label, "reader-dead-end",
                                             f"Reader mode showed no text ({(tg or {}).get('error')})"))
                        await js("document.querySelector('#ws-mode').click()")
                        await asyncio.sleep(0.4)
                    if not rd["back"]:
                        problems.append((label, "reader-dead-end", "the reader has no way back to the results"))
                    if not rd["original"]:
                        problems.append((label, "reader-dead-end",
                                         "no link to the original — the reader cannot parse every page"))
                    if not rd["readerOpen"]:
                        problems.append((label, "reader-dead-end",
                                         "PCWebSearch.readerOpen() is false with the reader open — the Android "
                                         "back button will leave the view instead of closing the article"))
                    ra = await js(AUDIT)
                    if ra and ra["overflow"]:
                        problems.append((label, "horizontal-overflow", "the reader scrolls the page sideways"))

                    bk = await js(BACK_TO_RESULTS, awaited=True)
                    if not bk or bk.get("error"):
                        problems.append((label, "reader-dead-end", f"back did not work ({(bk or {}).get('error')})"))
                    else:
                        if not bk["cards"]:
                            problems.append((label, "reader-dead-end", "back left no results on screen"))
                        if bk["readerOpen"]:
                            problems.append((label, "reader-dead-end", "back did not actually close the reader"))
                        if abs(bk["scroll"] - 350) > 60:
                            problems.append((label, "reader-scroll-lost",
                                             f"back landed at {round(bk['scroll'])}px, not the 350px the "
                                             "results were left at"))

                sv = await js(SAVE_AND_SHARE, awaited=True)
                if not sv or sv.get("error"):
                    problems.append((label, "missing-control", f"save/share did not run ({(sv or {}).get('error')})"))
                else:
                    if not sv["saved"] or "http" not in sv["savedBody"]:
                        problems.append((label, "missing-control", "Save to Notes wrote no note (or no link in it)"))
                    if not sv["shared"] or "http" not in sv["sharedText"]:
                        problems.append((label, "missing-control", "Share opened no composer with the link"))

        if problems:
            print(f"FAIL  {len(problems)} problem(s):")
            for label, kind, msg in problems:
                print(f"  [{label}] {kind}: {msg}")
            return 1
        print("OK  web search mobile checks passed")
        return 0
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="wscheck-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE)

    # The page view is an <iframe src="/api/websearch/page?url=…">, which is a real navigation and
    # never touches the stubbed window.fetch — so this server has to answer it or the frame is blank
    # and "did the page render" cannot be asked at all.
    with open(os.path.join(tmp, "page.html"), "w") as fh:
        fh.write("<!doctype html><meta charset=utf-8><title>Framed</title>"
                 "<body style='font:16px/1.5 system-ui;margin:0;padding:16px'>"
                 "<h1>The actual page</h1>" + "<p>Body text.</p>" * 40)

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            if path.startswith("/api/websearch/page"):
                return os.path.join(tmp, "page.html")
            return os.path.join(tmp, path.lstrip("/") or "index.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/index.html"
    try:
        return asyncio.run(drive(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
