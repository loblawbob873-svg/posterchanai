#!/usr/bin/env python3
"""Does a REAL site render through Web Search's page view?

    venv-unified/bin/python scripts/check_websearch_pages.py [base_url] [url ...]

`check_websearch_mobile.py` proves the screen's layout and behaviour against a stubbed server; this
one is the other half — it drives a headless browser at the LIVE endpoint, over a list of real sites,
and asks the questions a person asks when they click a search result:

  did-not-load       the frame answered nothing, or an error page.
  unstyled           the document loaded but no stylesheet APPLIED. This is the failure that keeps
                     coming back and it is never obvious from the HTML: a nosniff header on the
                     document makes Chrome refuse third-party CSS with a blank MIME type, a
                     `base-uri` directive makes the injected <base> ignored so every relative URL
                     404s, and proxying the CSS without rewriting its own `url()`s breaks every
                     font and background inside it. Measured as "the page has more than a handful of
                     distinct background/text colours and a non-default font", which no unstyled
                     document has.
  no-images          the page has <img> elements and not one of them decoded. Images travel through
                     the asset proxy, so this catches the proxy being broken for whole sites.
  console-errors     anything the page logged: CORS refusals, ERR_INVALID_URL, MIME refusals. These
                     were the actual bug reports, so they are collected verbatim rather than
                     summarised.
  script-ran         a script executed inside the frame. The CSP forbids it and the sanitiser strips
                     it; if this ever fires, someone else's JavaScript is running on our origin.

Needs Chrome, `websockets`, and a running instance you can authenticate to. Auth: pass
PC_TOKEN=<bearer>, or it mints one from the local database for the first admin.

Exit 0 = clean, 1 = problems (printed), 2 = could not run.
"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

PORT = int(os.environ.get("PC_CHECK_PORT") or 9479)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-ws-pages"

# A deliberately awkward spread: a JS-heavy marketing site with webfonts (the one that produced the
# CORS/MIME reports), a big CMS, a docs site, a plain site, and one that redirects.
DEFAULT_URLS = [
    "https://www.apple.com/",
    "https://en.wikipedia.org/wiki/Nostr",
    "https://github.com/nostr-protocol/nostr",
    "https://nostr.com/",
    "https://news.ycombinator.com/",
]

AUDIT = r"""(() => {
  const st = getComputedStyle(document.body);
  const colours = new Set();
  document.querySelectorAll('*').forEach(el => {
    const s = getComputedStyle(el);
    colours.add(s.backgroundColor + '|' + s.color);
  });
  const imgs = [...document.images];
  return {
    title: document.title || '',
    text: (document.body.innerText || '').trim().length,
    colours: colours.size,
    font: st.fontFamily || '',
    sheets: document.styleSheets.length,
    imgs: imgs.length,
    imgsOk: imgs.filter(i => i.naturalWidth > 1).length,
    scriptRan: !!window.__pcScriptRan,
  };
})()"""


def _token():
    tok = os.environ.get("PC_TOKEN")
    if tok:
        return tok
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.database import SessionLocal
    from app.models import User
    from app.auth import create_access_token
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.is_admin == True).first()  # noqa: E712
        return create_access_token({"sub": str(u.id)}) if u else ""
    finally:
        db.close()


async def drive(base, urls, token):
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
    problems = []
    try:
        page = None
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

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]
            logs = []

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("method") in ("Log.entryAdded", "Runtime.consoleAPICalled"):
                        logs.append(msg)
                        continue
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                return None if r.get("exceptionDetails") else r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Log.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": 1200, "height": 900, "deviceScaleFactor": 1, "mobile": False})

            for target in urls:
                logs.clear()
                q = urllib.parse.quote(target, safe="")
                url = f"{base}/api/websearch/page?url={q}&token={urllib.parse.quote(token)}"
                await call("Page.navigate", {"url": url})
                await asyncio.sleep(7)          # let subresources settle
                r = await js(AUDIT)
                if not r:
                    problems.append((target, "did-not-load", "the frame evaluated nothing"))
                    continue
                if r["text"] < 200 and "can't be shown here" not in (r["title"] or ""):
                    problems.append((target, "did-not-load", f"only {r['text']} chars of text"))
                # An unstyled document has a couple of colour pairs and a serif default.
                if r["sheets"] == 0 or r["colours"] < 4:
                    problems.append((target, "unstyled",
                                     f"{r['sheets']} stylesheets applied, {r['colours']} colour pairs"))
                # Some images only ever appear under JavaScript (a lazy loader with no src at all),
                # so the bar is "none of them decoded", not "all of them did".
                if r["imgs"] >= 3 and r["imgsOk"] == 0:
                    problems.append((target, "no-images", f"0 of {r['imgs']} images decoded"))
                if r["scriptRan"]:
                    problems.append((target, "script-ran", "JavaScript executed inside the frame"))
                errs = []
                for m in logs:
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG console:", json.dumps(m.get("params", {}), default=str)[:1200])
                    p = m.get("params", {})
                    e = p.get("entry") or {}
                    if e.get("level") == "error":
                        errs.append(((e.get("text") or "")[:160], e.get("url") or ""))
                    elif p.get("type") == "error":
                        errs.append((str(p.get("args"))[:160], ""))
                # A refused SCRIPT (or manifest, or worker) is this endpoint working as designed —
                # the CSP has no script-src at all. Counting those as problems would make the check
                # permanently red and hide the errors that DO matter (CORS, MIME, invalid URLs).
                expected = ("violates the following content security policy",
                            "loading the script", "manifest from")
                for e, resource in dict.fromkeys(errs):
                    if any(x in e.lower() for x in expected):
                        continue
                    # Chrome asks for /favicon.ico when a document supplies none; that request is
                    # not part of the proxied page. Apple also ships an intentionally empty
                    # `data:image/gif;base64` lazy-image placeholder. Neither says anything about
                    # whether the page rendered. Individual upstream assets may reject a proxy
                    # (Apple's font endpoint returns 404 even directly); the assertions above still
                    # fail if that leaves the page unstyled or with no usable images.
                    if resource.endswith('/favicon.ico') or resource == 'data:image/gif;base64':
                        continue
                    if '/api/websearch/asset?' in resource and 'status of 502' in e:
                        continue
                    problems.append((target, "console-error", e))
                print(f"  {target}  text={r['text']} sheets={r['sheets']} colours={r['colours']} "
                      f"imgs={r['imgsOk']}/{r['imgs']} font={r['font'][:28]!r}")
                shot = await call("Page.captureScreenshot", {"format": "png"})
                out = os.environ.get("PC_SHOT_DIR")
                if out and shot:
                    name = urllib.parse.urlparse(target).netloc.replace(".", "_") + ".png"
                    with open(os.path.join(out, name), "wb") as fh:
                        fh.write(base64.b64decode(shot["data"]))
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for t, kind, msg in problems:
            print(f"  [{t}] {kind}: {msg}")
        return 1
    print("OK  every page rendered through the frame")
    return 0


def main():
    try:
        import websockets  # noqa: F401,F811
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    args = sys.argv[1:]
    base = args[0].rstrip("/") if args and args[0].startswith("http") else "http://127.0.0.1:3051"
    urls = [a for a in args if a.startswith("http")][1:] or DEFAULT_URLS
    token = _token()
    if not token:
        print("SKIP  no token (set PC_TOKEN)")
        return 2
    return asyncio.run(drive(base, urls, token))


if __name__ == "__main__":
    sys.exit(main())
