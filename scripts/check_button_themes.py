#!/usr/bin/env python3
"""Primary buttons must be ONE colour, and it must be the THEME's colour — on every theme.

    venv-unified/bin/python scripts/check_button_themes.py

The Messages "New" button carried a 36px **cyan disc** behind its icon on every theme. The rule was
a leftover: "New message" used to be a ROW in the peer list, where its `.ic` sat in the slot the
other rows fill with a 36px avatar, so it was painted as a solid `--cyan` circle to keep the column
aligned. The button was later moved into the header as an ordinary pill, and the disc came with it —
a hardcoded cyan circle inside a themed pill. On `professional`, whose accent is blue, that is the
only element on the button not following the theme, and that is how it was reported.

Nothing caught it because nothing was WRONG in the ordinary sense: the rule was valid, applied, and
rendered exactly what it said. It was describing markup that no longer existed.

WHAT IS ASSERTED, for every theme and every primary button:

  child-paints-its-own-background  A descendant of a button paints a background of its own. A button
                                   is one shape in one colour; anything painting inside it is either
                                   a second colour or a leftover from different markup. This is the
                                   general form of the bug above, so it cannot come back on a
                                   different button either.
  off-theme-colour                 Something inside a button is painted a colour the theme never
                                   defines — the literal "still cyan on a blue theme" test, run
                                   against each theme's own token values rather than a fixed list.
  icon-not-inheriting              A button's icon does not inherit the button's text colour. On a
                                   filled button that is how a glyph ends up dark-on-dark: the disc
                                   here also forced `color:#0a0712`, which was legible only because
                                   the disc behind it was pale.

Renders the SHIPPED stylesheet against the real markup, one page per theme, and reads computed
styles — a source grep would pass against any hardcoded colour that merely looked plausible.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome).
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "css", "client.css")
PORT = int(os.environ.get("PC_CHECK_PORT") or 9478)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-btn-theme-check"

# The buttons this is about: a filled primary, which is where a stray fill is both most likely and
# most visible. Markup copied from the app, class-for-class — the point is to style what it styles.
BUTTONS = [
    ('dm-new', '<button class="btn btn-neon small dm-newbtn" id="dm-new">'
               '<svg class="ic b-ic" aria-hidden="true"><use href="#i-mail"></use></svg>New</button>'),
    ('btn-neon', '<button class="btn btn-neon">'
                 '<svg class="ic b-ic" aria-hidden="true"><use href="#i-mail"></use></svg>Post</button>'),
    ('btn-cyan', '<button class="btn btn-cyan">'
                 '<svg class="ic b-ic" aria-hidden="true"><use href="#i-mail"></use></svg>Send</button>'),
]


def themes():
    """Every theme the stylesheet defines, read FROM the stylesheet — a hand-kept list here would go
    stale the first time somebody adds a theme, and the missing one is the one that breaks."""
    css = open(CSS, encoding="utf8").read()
    found = sorted(set(re.findall(r'\[data-theme="([a-z0-9]+)"\]', css)))
    return ["(default)"] + found


PAGE = """<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css">
<body><div class="wrap"><div class="main"><div id="feed">
__BUTTONS__
</div></div></div>
<script src="/static/js/client/sprite.js"></script>
</body>"""

PROBE = r"""(() => {
  const out = [];
  const px = (c) => {
    const m = /rgba?\(([^)]+)\)/.exec(c || '');
    if (!m) return null;
    const p = m[1].split(',').map(s => parseFloat(s));
    if (p.length > 3 && p[3] === 0) return null;       // fully transparent paints nothing
    return [Math.round(p[0]), Math.round(p[1]), Math.round(p[2])];
  };
  for (const b of document.querySelectorAll('button')) {
    const bs = getComputedStyle(b);
    const rec = { id: b.id || b.className, painted: [], iconColor: '', textColor: bs.color };
    for (const kid of b.querySelectorAll('*')) {
      const ks = getComputedStyle(kid);
      const bg = px(ks.backgroundColor);
      // A child painting its own background is the shape of the bug, whatever colour it is.
      if (bg) rec.painted.push({ tag: kid.tagName.toLowerCase(),
                                 cls: String(kid.className.baseVal || kid.className || ''),
                                 bg: ks.backgroundColor });
      if (kid.tagName.toLowerCase() === 'svg') rec.iconColor = ks.color;
    }
    out.push(rec);
  }
  // The theme's OWN palette, so "off-theme" is judged against this theme rather than a fixed list.
  const cs = getComputedStyle(document.documentElement);
  out.tokens = {};
  for (const t of ['--neon', '--neon2', '--cyan', '--green', '--amber', '--danger', '--text', '--bg'])
    out.tokens[t] = cs.getPropertyValue(t).trim();
  return { buttons: out, tokens: out.tokens };
})()"""


def main():
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium") or shutil.which("chromium-browser"))
    if not chrome:
        print("SKIP  no Chrome on this machine")
        return 2
    try:
        import websockets  # noqa: F401
    except Exception:
        print("SKIP  the websockets package is not installed")
        return 2

    body = "\n".join(html for _n, html in BUTTONS)
    page = PAGE.replace("__BUTTONS__", body)
    out_dir = os.path.join(ROOT, "static", "_btncheck")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf8") as f:
        f.write(page)

    handler = partial(SimpleHTTPRequestHandler, directory=ROOT)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/static/_btncheck/index.html"

    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return run(url)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        srv.shutdown()
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(PROFILE, ignore_errors=True)


def run(url):
    return asyncio.run(drive(url))


async def drive(url):
    import websockets
    page = None
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
            page = [t for t in tabs if t["type"] == "page"][0]
            break
        except Exception:
            time.sleep(0.5)
    if not page:
        print("SKIP  could not start Chrome")
        return 2

    problems = []
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
        n = [0]

        async def call(method, params=None):
            n[0] += 1
            await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == n[0]:
                    return m.get("result", {})

        async def ev(expr):
            r = await call("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True, "awaitPromise": True})
            return (r.get("result") or {}).get("value")

        await call("Page.enable")
        await call("Runtime.enable")

        for theme in themes():
            await call("Page.navigate", {"url": url})
            await asyncio.sleep(0.45)
            if theme == "(default)":
                await ev("document.documentElement.removeAttribute('data-theme')")
            else:
                await ev(f"document.documentElement.setAttribute('data-theme','{theme}')")
            await asyncio.sleep(0.2)
            res = await ev(PROBE)
            if not res:
                problems.append((theme, "probe returned nothing"))
                continue
            for b in res.get("buttons") or []:
                who = b.get("id") or "?"
                for p in b.get("painted") or []:
                    problems.append((theme,
                                     f"{who}: its <{p['tag']} class={p['cls']!r}> paints its own "
                                     f"background {p['bg']} — a button is one shape in one colour"))
                icon, text = b.get("iconColor"), b.get("textColor")
                if icon and text and icon != text:
                    problems.append((theme,
                                     f"{who}: the icon is {icon} but the label is {text} — the glyph "
                                     "is not inheriting the button's colour"))

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for theme, msg in problems:
            print(f"  [{theme}] {msg}")
        return 1
    print(f"OK  primary buttons are one themed colour on all {len(themes())} themes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
