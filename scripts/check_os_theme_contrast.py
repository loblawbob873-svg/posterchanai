#!/usr/bin/env python3
"""The windowed desktop's own chrome, READ FOR CONTRAST on every theme.

    venv-unified/bin/python scripts/check_os_theme_contrast.py

Why this exists. `.os-app` is the start menu's app row and `.os-task` is the taskbar's window
button, and the PosterChanOS shell block further down client.css reused BOTH class names for its own
launcher. Being later in the file with equal specificity it won every property the two rules shared
— including `color`, which it set to `var(--fg, #d6e2ff)`. `--fg` is declared in no rule in this
stylesheet, so that resolved to the literal on EVERY theme. Measured before the fix: the start-menu
labels sat at 1.14-1.40:1 against their own panel on the five light themes (Cherry Blossom 1.22,
Professional 1.21, Windows 98 1.40, Windows XP 1.14, Anime Girl 1.17). Pale blue on near-white is a
start menu that opens, is full of apps, and reads as blank — which is exactly how it was reported.

Nothing catches that except measurement: the markup is right, the list is populated, no console says
anything, and the default (dark) theme looks perfect, which is the theme every other check runs on.

  startmenu-unreadable   a start-menu row's text against the desktop background
  taskbar-unreadable     a taskbar window button's text
  traypop-unreadable     a tray popover row's text against the popover
  hidden-row             a row that computes to display:none / zero height / zero opacity — the
                         other way a full menu draws as an empty one

WCAG AA for body text is 4.5:1; the floor here is 3.0, which is the large-text bar and generous —
the failures this exists for are around 1.2, not around 4.

  theme-ignored          a desktop style whose chrome does not follow the palette at all

CONTRAST ALONE CANNOT ASK THAT SECOND QUESTION, and the macOS style is why the row exists. It was
authored as a fixed dark glass — a #17202d desk, rgba(31,34,42,.72) surfaces, white text — so
choosing it replaced the user's theme with somebody else's. White on near-black scores ~15:1 on
EVERY theme, so it passed contrast perfectly while ignoring the palette completely; and this check
only ever ran the default style, so it never even looked. `theme-ignored` compares the chrome's
painted backdrop against the theme's own --bg and fails when they are further apart than
MAX_THEME_DRIFT in luminance — the one measurement that separates "unreadable" from "wrong colours".

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / no websockets).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9497)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-theme-check"
HTTP_PORT = 0
MIN_RATIO = 3.0

# Every theme slug app.js offers. Cyberpunk is the bare :root and carries no data-theme attribute.
THEMES = ["cyberpunk", "cherryblossom", "professional", "win98", "winxp",
          "animegirl", "sovietgothic", "dark", "monero"]

# The desktop's own chrome, written the way os.js writes it. Hand-built rather than booted through
# the whole client on purpose: this is a question about the STYLESHEET, and a page that cannot fail
# to log in cannot fail for a reason that has nothing to do with the thing being measured.
PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css"></head>
<body class="os-on"><div class="os-root os-on" id="os-root">
  <div class="os-desk" id="os-desk"></div>
  <div class="os-startmenu" id="os-startmenu">
    <input class="input os-search" id="os-q" placeholder="Search apps">
    <div class="os-applist" id="os-applist">
      <button class="os-app" data-view="home"><svg class="ic"></svg><span>Home</span></button>
      <div class="os-applist-h">This computer</div>
      <button class="os-app" data-app="app:firefox"><svg class="ic"></svg><span>Browser</span></button>
    </div>
    <div class="os-foot"><span class="spacer"></span></div>
  </div>
  <div class="os-bar" id="os-bar">
    <div class="os-tasks"><button class="os-task on"><svg class="ic"></svg><span>Social</span></button></div>
  </div>
  <div class="os-pop"><div class="os-pop-h">Network</div>
    <div class="os-pop-b"><button class="os-pop-row"><span class="os-pop-nm">home-wifi</span>
      <span class="os-pop-sig">88%</span></button></div>
    <div class="os-pop-f"><button class="os-pop-btn">More</button></div></div>
  <!-- The macOS chrome: a menu bar, the machine's own tray chip, the clock and a window title.
       All four sit on GLASS, which is the surface that used to be a fixed dark grey. -->
  <div class="os-mac-menu"><button>PosterChan</button><button>File</button></div>
  <div class="os-tray"><div class="os-sys"><button class="os-chip">Wi-Fi</button></div>
    <div class="os-clock"><b>10:42</b><span>Sun</span></div></div>
  <div class="osw focused" style="left:80px;top:80px;width:420px;height:220px">
    <div class="osw-bar"><div class="osw-btns"><button class="osw-b" data-w="close"></button></div>
      <span class="osw-title">Documents</span></div>
    <div class="osw-body"></div></div>
</div></body></html>"""

# Contrast is measured against the first ANCESTOR that actually paints — a transparent panel over a
# wallpaper is read through, and reading the element's own background would score a transparent row
# against itself and call every theme perfect.
MEASURE = r"""(() => {
  /* COLOURS ARE PARSED BY A CANVAS, NOT BY A REGEX. getComputedStyle now hands back `color-mix(in
     srgb, …)` and `oklab(…)` verbatim for anything authored that way — half this stylesheet — and a
     /[\d.]+/ over that reads lightness and chroma as if they were r,g,b. That scores a perfectly
     readable taskbar at 1:1 and calls it a bug. The 2D context resolves any CSS colour string to
     real sRGB bytes with its alpha, which is what a person actually sees. */
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const parse = (s) => {
    try {
      cx.clearRect(0, 0, 1, 1);
      cx.fillStyle = '#000'; cx.fillStyle = s;      // an unparsable string leaves the previous value
      cx.globalCompositeOperation = 'copy';
      cx.fillRect(0, 0, 1, 1);
      const d = cx.getImageData(0, 0, 1, 1).data;
      return [d[0], d[1], d[2], d[3] / 255];
    } catch (_) { return [0, 0, 0, 0]; }
  };
  const over = (top, bot) => {                       // source-over, both already sRGB
    const a = top[3];
    return [Math.round(top[0]*a + bot[0]*(1-a)), Math.round(top[1]*a + bot[1]*(1-a)),
            Math.round(top[2]*a + bot[2]*(1-a)), 1];
  };
  const lum = (c) => { const [r,g,b] = c.slice(0,3).map(v => { v/=255;
      return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); });
    return 0.2126*r + 0.7152*g + 0.0722*b; };
  /* The backdrop is every painting ancestor composited in paint order — an element with a 5%-white
     background over a translucent taskbar over the wallpaper is three layers, and reading only the
     nearest one scores a transparent row against itself. */
  const backdrop = (el) => {
    const stack = [];
    for (let n = el; n && n !== document.documentElement; n = n.parentElement)
      stack.push(parse(getComputedStyle(n).backgroundColor));
    stack.push(parse(getComputedStyle(document.documentElement).backgroundColor));
    stack.push([255, 255, 255, 1]);                  // the canvas the page is painted on
    let acc = stack[stack.length - 1];
    for (let i = stack.length - 2; i >= 0; i--) acc = over(stack[i], acc);
    return acc;
  };
  const read = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return { missing: true };
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    const bg = backdrop(el);
    const fg = over(parse(cs.color), bg);
    const a = lum(fg), b = lum(bg);
    return { ratio: Math.round(((Math.max(a,b)+0.05)/(Math.min(a,b)+0.05))*100)/100,
             color: cs.color, fg: fg.slice(0,3).join(','), bg: bg.slice(0,3).join(','),
             display: cs.display, opacity: Number(cs.opacity),
             w: Math.round(r.width), h: Math.round(r.height) };
  };
  const out = { startmenu: read('#os-applist .os-app'),
                taskbar:   read('.os-tasks .os-task'),
                traypop:   read('.os-pop .os-pop-row') };
  /* The macOS surfaces are measured ONLY under that style: without the class they are display:none
     (`.os-mac-menu{display:none}`), and reading a hidden element would report every theme broken. */
  if (document.getElementById('os-root').classList.contains('os-style-mac')) {
    out.macmenu   = read('.os-mac-menu button');
    out.macchip   = read('.os-sys .os-chip');
    out.macclock  = read('.os-clock b');
    out.macwindow = read('.osw .osw-title');
  }
  /* The theme's OWN base colour, read from the custom property rather than from any painted
     element, so it is the same question whichever desktop style is on. */
  out.themeBg = { raw: parse(getComputedStyle(document.documentElement)
                     .getPropertyValue('--bg').trim()).slice(0,3).join(',') };
  return out;
})()"""


# How far the chrome may sit from the theme's own background, in relative luminance.
# NOT a light/dark verdict: Windows 98's palette is mid-grey (--bg #c0c0c0, --bg2 #b8b8b0) and
# straddles any 0.5 line by 8/255, so a side-of-the-divide test failed it three times for a
# difference nobody can see. Distance asks the real question. Measured: the old hard-coded glass sat
# ~0.9 from every light theme; the theme-derived one sits under 0.1 on all nine.
MAX_THEME_DRIFT = 0.35


def _lum(rgb):
    """Relative luminance — the same sRGB curve the in-page measurement uses."""
    try:
        r, g, b = [int(x) for x in str(rgb).split(",")[:3]]
    except Exception:
        return 0.0
    def c(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * c(r) + 0.7152 * c(g) + 0.0722 * c(b)


def serve():
    global HTTP_PORT

    class H(SimpleHTTPRequestHandler):
        def translate_path(self, p):
            return os.path.join(ROOT, p.lstrip("/").split("?")[0])

        def do_GET(self):
            if self.path.startswith("/static/"):
                return super().do_GET()
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    HTTP_PORT = httpd.server_address[1]
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
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=1 << 24) as ws:
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
        await call("Emulation.setDeviceMetricsOverride",
                   {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await call("Page.navigate", {"url": f"http://127.0.0.1:{HTTP_PORT}/"})
        await asyncio.sleep(2)
        if not await js("!!document.querySelector('#os-applist .os-app')"):
            print("SKIP  the harness page did not render")
            return 2
        # BOTH DESKTOP STYLES, because the macOS one was written as a fixed dark glass and this
        # check only ever ran the default. White on near-black scores ~15:1 on every theme, so the
        # style could ignore the palette completely and still pass — which is exactly what it did.
        for style in ("posterchan", "mac"):
            await js("document.getElementById('os-root').classList.%s('os-style-mac')"
                     % ("add" if style == "mac" else "remove"))
            for theme in THEMES:
                label = theme if style == "posterchan" else f"{theme}/mac"
                await js("document.documentElement." + ("removeAttribute('data-theme')"
                                                        if theme == "cyberpunk"
                                                        else "setAttribute('data-theme','%s')" % theme))
                await asyncio.sleep(0.25)
                got = await js(MEASURE)
                if not isinstance(got, dict) or got.get("__throw"):
                    problems.append(f"{label}: measurement did not evaluate ({got})")
                    continue
                base = got.pop("themeBg", {}).get("raw") or "0,0,0"
                for name, res in got.items():
                    if res.get("missing"):
                        problems.append(f"{label}: {name}: the row is not in the page at all")
                        continue
                    if res["display"] == "none" or res["h"] == 0 or res["opacity"] < 0.2:
                        problems.append(f"hidden-row {label}: {name} computes to "
                                        f"display:{res['display']} h:{res['h']}px "
                                        f"opacity:{res['opacity']} — a full menu that draws as empty")
                    # Does this surface live on the same side of the divide as the theme?
                    # Asked of the styled chrome only: the default desktop IS the palette.
                    if style == "mac" and name.startswith("mac") and not res.get("missing"):
                        drift = abs(_lum(res["bg"]) - _lum(base))
                        if drift > MAX_THEME_DRIFT:
                            problems.append(
                                f"theme-ignored {label}: {name} paints {res['bg']} while the theme's "
                                f"--bg is {base} — {drift:.2f} apart in luminance, over the "
                                f"{MAX_THEME_DRIFT} ceiling. Choosing a desktop style must not "
                                "replace the palette.")
                    if res["ratio"] < MIN_RATIO:
                        problems.append(f"{name}-unreadable {label}: {res['ratio']}:1 "
                                        f"(text {res['fg']} on {res['bg']}) — below the "
                                        f"{MIN_RATIO}:1 floor. A hard-coded colour that ignores the "
                                        "theme reads as a blank panel.")
    return 1 if problems else 0


async def run():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    if not shutil.which("google-chrome-stable"):
        print("SKIP  google-chrome-stable not found")
        return 2
    httpd = serve()
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
            print("\ndesktop chrome: readable on every theme")


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
