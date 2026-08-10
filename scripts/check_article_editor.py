#!/usr/bin/env python3
"""Layout check for the ARTICLE EDITOR's side-by-side preview, at phone and desktop widths.

Run BEFORE deploying a change to the article editor:

    venv-unified/bin/python scripts/check_article_editor.py

check_client_mobile.py runs as a guest against the live site and never opens this screen, so a split
view that is unusable on a phone would ship having "passed the mobile check".

THE MARKUP IS NOT WRITTEN HERE. It is lifted out of the shipped app.js by matching the editor's own
template literal, so a rename fails the check instead of quietly testing a copy — the same rule
scripts/check_files_explorer.py follows, and for the same reason: this repo has twice shipped a
static repro that passed against CSS the real screen was broken under.

Assertions, each a way a two-pane editor goes wrong:

  horizontal-overflow   the page scrolls sideways. Two columns at 390px is the default failure.
  panes-not-side-by-side   on a desktop the two panes must share a row. If they stack, the split
                        view silently became the old one-at-a-time editor.
  both-panes-on-a-phone the whole point of the tabs: at 390px only one pane may be on screen, and
                        the hidden one must be display:none rather than merely narrow, or it still
                        pushes the page sideways.
  tabs-wrong-width      the Write/Preview tabs exist only to choose between panes, so they must be
                        HIDDEN where both are visible and PRESENT where only one is.
  panes-unequal         one pane taller than the other reads as a broken layout; they are sized by
                        the pane, not the textarea, precisely so they cannot drift.
  no-scroll             a pane that grows with its content instead of scrolling turns a long article
                        into a page kilometres tall with the toolbar off the top.
  tiny-tap-target       a tab under 32px on a phone.
  ios-zoom-trap         the textarea under 16px on a phone: iOS zooms on focus and never zooms back.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / websockets).
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
WIDTHS = [(390, 844, True), (360, 780, True), (900, 800, False), (1280, 860, False)]
PORT = 9491
PROFILE = "/tmp/pc-article-editor-check"


def editor_markup():
    """The editor's own template literal, out of app.js, with its ${...} holes filled in."""
    src = open(APP, encoding="utf-8").read()
    m = re.search(r'feed\.innerHTML=`(<div class="article-editor">.*?)`;\n', src, re.S)
    if not m:
        raise SystemExit("could not find the article editor's template in app.js — if it was "
                         "restructured, update this check rather than letting it test nothing.")
    html = m.group(1)
    if "ae-split" not in html:
        raise SystemExit("the editor template has no .ae-split — the side-by-side view is gone")
    # `${enc(...)}` → a plausible value; `${...}` → empty. The layout is what is under test.
    html = re.sub(r"\$\{enc\(g\('title'\)\)\}", "A reasonably long article title", html)
    html = re.sub(r"\$\{enc\(g\('summary'\)\)\}", "One line of summary", html)
    html = re.sub(r"\$\{[^}]*\}", "", html)
    return html


BODY = ("# Heading\n\nSome **markdown** body text that is long enough to need scrolling.\n\n"
        + "\n\n".join("Paragraph %d with enough words in it to wrap on a narrow pane." % i
                      for i in range(40)))
PREVIEW = "<h1>Heading</h1>" + "".join(
    "<p>Paragraph %d with enough words in it to wrap on a narrow pane.</p>" % i for i in range(40))

PAGE_TMPL = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="feed">__EDITOR__</div>
<nav class="mobilenav glass"><button class="nav-item"><b>Home</b></button></nav>
<script src="/static/js/client/sprite.js"></script>
<script>
document.getElementById('ae-body').value = __BODY__;
document.getElementById('ae-preview').innerHTML = __PREVIEW__;
// The one behaviour under test: the tabs toggle a class on the split, nothing else.
document.querySelectorAll('.cmp-tab').forEach(b => b.onclick = () => {
  document.querySelectorAll('.cmp-tab').forEach(x => x.classList.toggle('active', x === b));
  document.getElementById('ae-split').classList.toggle('show-preview', b.dataset.t === 'preview');
});
window.__ready = true;
</script>
</body></html>"""

AUDIT = r"""(() => {
  const vw = window.innerWidth;
  const box = el => { const r = el.getBoundingClientRect(); return {x:r.x, y:r.y, w:r.width, h:r.height, right:r.right}; };
  const vis = el => !!(el && el.getClientRects().length && getComputedStyle(el).display !== 'none'
                       && getComputedStyle(el).visibility !== 'hidden');
  const out = { vw, overflow: document.documentElement.scrollWidth > vw + 1 };
  const split = document.getElementById('ae-split');
  const panes = Array.from(document.querySelectorAll('.ae-pane'));
  out.panes = panes.length;
  const shown = panes.filter(vis);
  out.shown = shown.length;
  if(shown.length === 2){
    const a = box(shown[0]), b = box(shown[1]);
    out.sideBySide = Math.abs(a.y - b.y) < 2 && b.x > a.x + 10;
    out.heightDelta = Math.round(Math.abs(a.h - b.h));
  }
  out.tabsVisible = vis(document.querySelector('.ae-tabs'));
  const ta = document.getElementById('ae-body'), pv = document.getElementById('ae-preview');
  out.taFont = ta ? parseFloat(getComputedStyle(ta).fontSize) : 0;
  // A pane must SCROLL its content, not grow to fit it.
  out.taScrolls = !!ta && ta.scrollHeight > ta.clientHeight + 4;
  out.pvScrolls = !!pv && pv.scrollHeight > pv.clientHeight + 4;
  out.taH = ta ? Math.round(box(ta).h) : 0;
  out.small = [];
  for(const el of document.querySelectorAll('.cmp-tab')){
    if(!vis(el)) continue;
    const h = box(el).h;
    if(h < 32) out.small.push({ text:(el.textContent||'').trim().slice(0,14), h:Math.round(h) });
  }
  // Nothing may reach past the editor column.
  const ed = document.querySelector('.article-editor');
  out.wide = ed ? Array.from(document.querySelectorAll('.ae-pane'))
    .filter(p => vis(p) && box(p).right > box(ed).right + 1).length : 0;
  return out;
})()"""

TAP_PREVIEW = """(() => { const b = document.querySelector('.cmp-tab[data-t="preview"]');
                         if(!b) return false; b.click(); return true; })()"""


def judge(label, r, phone, tab):
    bad = []
    if r["overflow"]:
        bad.append(f"[horizontal-overflow] {label}: the page scrolls sideways")
    if r["panes"] != 2:
        bad.append(f"[panes-not-side-by-side] {label}: found {r['panes']} panes, expected 2")
        return bad
    if phone:
        if r["shown"] != 1:
            bad.append(f"[both-panes-on-a-phone] {label}: {r['shown']} panes on screen — the hidden "
                       "one must be display:none, not merely narrow")
        if not r["tabsVisible"]:
            bad.append(f"[tabs-wrong-width] {label}: only one pane is shown and there are no tabs to "
                       "reach the other")
        if r["taFont"] and r["taFont"] < 16 and tab == "write":
            bad.append(f"[ios-zoom-trap] {label}: the textarea is {r['taFont']}px; iOS zooms on focus "
                       "and never zooms back")
        for s in r["small"]:
            bad.append(f"[tiny-tap-target] {label}: “{s['text']}” is {s['h']}px")
    else:
        if r["shown"] != 2:
            bad.append(f"[panes-not-side-by-side] {label}: {r['shown']} pane(s) visible — the split "
                       "view has silently become the old one-at-a-time editor")
        elif not r.get("sideBySide"):
            bad.append(f"[panes-not-side-by-side] {label}: the panes are stacked, not in a row")
        elif r.get("heightDelta", 0) > 4:
            bad.append(f"[panes-unequal] {label}: the panes differ by {r['heightDelta']}px in height")
        if r["tabsVisible"]:
            bad.append(f"[tabs-wrong-width] {label}: both panes are visible and the Write/Preview "
                       "tabs are still shown, where they mean nothing")
    if tab == "write" and not r["taScrolls"]:
        bad.append(f"[no-scroll] {label}: the textarea grows with its content instead of scrolling")
    if r["wide"]:
        bad.append(f"[horizontal-overflow] {label}: {r['wide']} pane(s) reach past the editor column")
    return bad


async def drive_browser(url):
    import websockets
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium") or shutil.which("chromium-browser"))
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

            async def js(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                if r.get("exceptionDetails"):
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for w, h, phone in WIDTHS:
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2 if phone else 1,
                            "mobile": phone})
                await call("Page.navigate", {"url": url})
                ok = False
                for _ in range(60):
                    await asyncio.sleep(0.2)
                    if await js("window.__ready === true"):
                        ok = True
                        break
                if not ok:
                    print(f"SKIP  {w}px: the page never rendered")
                    return 2
                await asyncio.sleep(0.15)
                r = await js(AUDIT)
                if r is None:
                    print(f"SKIP  {w}px: page did not evaluate")
                    return 2
                problems += judge(f"{w}px/write", r, phone, "write")
                # ...and with Preview selected, which on a phone is the other half of the feature.
                if phone:
                    await js(TAP_PREVIEW)
                    await asyncio.sleep(0.15)
                    r2 = await js(AUDIT)
                    if r2:
                        problems += judge(f"{w}px/preview", r2, phone, "preview")

        if problems:
            print("\nArticle editor — regressions:\n")
            for p in problems:
                print("  " + p)
            return 1
        print("Article editor: clean at " + ", ".join(f"{w}px" for w, _, _ in WIDTHS)
              + " · write and preview")
        return 0
    finally:
        proc.terminate()


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="aecheck-")
    page = (PAGE_TMPL.replace("__EDITOR__", editor_markup())
                     .replace("__BODY__", json.dumps(BODY))
                     .replace("__PREVIEW__", json.dumps(PREVIEW)))
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(page)

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(tmp, path.lstrip("/") or "index.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/index.html"
    try:
        return asyncio.run(drive_browser(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
