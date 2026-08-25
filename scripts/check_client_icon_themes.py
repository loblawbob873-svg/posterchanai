#!/usr/bin/env python3
"""Every sprite icon must RENDER in every theme.

    venv-unified/bin/python scripts/check_client_icon_themes.py [base_url]

The icon sprite strokes `currentColor`, which is what lets one set of symbols serve all nine themes
with no per-theme CSS. But a <use> pointing at a symbol that does not exist renders NOTHING — 0x0,
silently, with no console error — and check_client_icons.py can only prove the symbol is DEFINED, not
that it survives a given theme's layout. So this renders the client under every theme and measures.

This deliberately does NOT check colour contrast. Two attempts at it were built and thrown away: a
computed-style walk cannot see through a `background: linear-gradient(...)`, which reports
backgroundColor as transparent, so it measures the icon against whatever panel is BEHIND its actual
surface. That called #btn-install (white on a blue gradient) "1.07:1 invisible" in four themes, and
after depth-limiting the walk it called the winxp sidebar's nav icons invisible for the same reason
(that sidebar is a blue gradient more than four levels up). Both were wrong, and a check that cries
wolf is worse than no check. Contrast needs real pixel sampling from a screenshot; until then it is
not claimed here.

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / site unreachable).
"""
import os
import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9226)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-icon-theme-profile"

THEMES = ["cyberpunk", "professional", "cherryblossom", "win98", "winxp",
          "dark", "monero", "sovietgothic", "animegirl"]
AUDIT = r"""
(() => {
  // checkVisibility(), NOT offsetParent — offsetParent is always null on an SVG, which would make
  // every legitimately hidden icon look like a bug.
  const zero = [];
  for (const el of document.querySelectorAll('svg.ic')) {
    if (!el.checkVisibility || !el.checkVisibility()) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) {
      zero.push({sym: (el.querySelector('use')?.getAttribute('href') || '?'),
                 cls: el.className.baseVal || ''});
    }
  }
  return {total: document.querySelectorAll('svg.ic').length, zero};
})()
"""


async def drive():
    import websockets
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

    problems, rendered = [], {}
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
        await call("Emulation.setDeviceMetricsOverride",
                   {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await call("Page.navigate", {"url": BASE + "/client"})
        await asyncio.sleep(12)

        for theme in THEMES:
            # Theme CSS is selected by the root data attribute; it does not require a navigation.
            # Reloading once per theme made this compare nine DIFFERENT live timelines. The first
            # relay fill had 466 icons and the last 1019, which was reported as a theme layout
            # failure even though every rendered icon had a valid size. Audit one frozen DOM and
            # change only the variable under test.
            await js(f"(() => {{ localStorage.setItem('pc_theme','{theme}'); "
                     f"document.documentElement.dataset.theme='{theme}'; return true; }})()")
            await asyncio.sleep(0.25)
            res = await js(AUDIT)
            if res is None:
                print(f"SKIP  {theme}: page did not evaluate (site unreachable?)")
                return 2
            for z in res["zero"]:
                problems.append((theme, "zero-sized-icon", f"{z['sym']} ({z['cls']})"))
            rendered[theme] = res["total"]
            print(f"  {theme:14} icons={res['total']:5}  zero-sized={len(res['zero'])}")

    # A theme that renders far fewer icons than the rest did not "pass" — it failed to lay out.
    if rendered:
        lo, hi = min(rendered.values()), max(rendered.values())
        if hi and lo < hi * 0.5:
            thin = ", ".join(f"{k}={v}" for k, v in rendered.items() if v < hi * 0.5)
            problems.append(("-", "theme-rendered-few-icons", f"{thin} (peak {hi})"))
    if not problems:
        print(f"OK  every icon renders in all {len(THEMES)} themes")
        return 0
    print(f"\nFAIL  {len(problems)} problem(s)")
    seen = set()
    for theme, kind, msg in problems:
        key = (kind, msg)
        if key in seen:
            continue
        seen.add(key)
        print(f"        {theme:14} {kind:16} {msg}")
    return 1


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    if not shutil.which("google-chrome-stable"):
        print("SKIP  no Chrome on this node")
        return 2
    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return await drive()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)   # /tmp is tmpfs here — a left profile is held RAM


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
