#!/usr/bin/env python3
"""The composer button rows, MEASURED — run before adding or relabelling a composer button.

    venv-unified/bin/python scripts/check_composer_toolbar.py

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome).

WHY THIS EXISTS SEPARATELY FROM check_client_mobile.py: that one drives the live site as a GUEST, and
a guest gets the sign-up card where the composer would be. Neither toolbar is on the page, so it has
never been able to see either of them — and the inline row carries the tightest layout budget in the
client. Its own comment records the rule in pixels:

    "Five icons + Post is exactly what fits one row at 360px (~269px of 279px); a sixth wraps Post
     onto its own line"

which is a measurement nothing was actually measuring. A sixth icon is a one-character diff that
silently pushes Post onto its own line on the narrowest common phone.

This renders the REAL markup — extracted from app.js, not retyped — against the REAL stylesheet at
the real widths, and asserts:

  * the inline row keeps Post on the first line (the documented budget), and fits its width;
  * every button in both rows is tappable (>= 28px on both axes: below that a labelled control is
    being crushed by a flex parent);
  * no button's icon has collapsed to nothing, the sprite failure that draws an empty oval;
  * neither row overflows its container sideways at any width.

The modal row is ALLOWED to wrap — it is a wrapping row of labelled buttons by design. The inline one
is not, and that is the difference the two sets of assertions encode.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")
SPRITE = os.path.join(ROOT, "static", "js", "client", "sprite.js")
PORT = 9473
PROFILE = "/tmp/pc-composer-check"
# 360 is the documented budget; 390 is the other common phone; 1280 is the desktop modal.
WIDTHS = [(360, 780), (390, 844), (1280, 900)]
MIN_TAP = 28


def _strip_templates(html: str) -> str:
    """Resolve the Jinja-ish `${cond?'':'…'}` branches to the WORST CASE — every button present.

    A top-level post shows the most buttons (a reply hides Poll and Background), so that is the
    layout that has to fit. Repeated until stable because the branches nest.
    """
    pat = re.compile(r"\$\{[^{}]*?\?\s*''\s*:\s*([`'])(.*?)\1\s*\}", re.S)
    for _ in range(10):
        new = pat.sub(lambda m: m.group(2), html)
        if new == html:
            break
        html = new
    # Anything left is a value interpolation, not a branch — drop it rather than render "${enc(av)}".
    return re.sub(r"\$\{.*?\}", "", html, flags=re.S)


def _extract(src: str, cls: str) -> str:
    """The innerHTML of the FIRST <div class="cls"> … balanced </div> in app.js."""
    start = src.index(f'<div class="{cls}">')
    i = src.index(">", start) + 1
    depth, j = 1, i
    while depth:
        nxt_open = src.find("<div", j)
        nxt_close = src.find("</div>", j)
        if nxt_close < 0:
            raise SystemExit(f"check_composer_toolbar: unbalanced markup for .{cls}")
        if 0 <= nxt_open < nxt_close:
            depth += 1
            j = nxt_open + 4
        else:
            depth -= 1
            j = nxt_close + 6
    return _strip_templates(src[i:j - 6])


AUDIT = r"""(() => {
  const out = {rows: {}, problems: []};
  for (const cls of ['tl-cmp-tools', 'cmp-tools']) {
    const row = document.querySelector('.' + cls);
    if (!row) { out.problems.push(cls + ': not rendered'); continue; }
    const rr = row.getBoundingClientRect();
    const btns = [...row.querySelectorAll('button')].map(b => {
      const r = b.getBoundingClientRect();
      const sv = b.querySelector('svg.ic');
      const sr = sv ? sv.getBoundingClientRect() : null;
      return {id: b.id || b.textContent.trim().slice(0, 12), x: Math.round(r.left),
              y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
              icon: sr ? Math.round(Math.min(sr.width, sr.height)) : -1};
    });
    out.rows[cls] = {w: Math.round(rr.width), scroll: row.scrollWidth,
                     client: row.clientWidth, btns: btns};
  }
  return out;
})()"""


async def drive(ws_url, page_html):
    import websockets  # noqa: F401  (used via websockets.connect below)
    problems = []
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        n = [0]

        async def cmd(method, **params):
            n[0] += 1
            await ws.send(json.dumps({"id": n[0], "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n[0]:
                    return msg.get("result", {})

        await cmd("Page.enable")
        for w, h in WIDTHS:
            await cmd("Emulation.setDeviceMetricsOverride", width=w, height=h,
                      deviceScaleFactor=1, mobile=w < 800)
            await cmd("Page.navigate", url="data:text/html;charset=utf-8," +
                      urllib.parse.quote(page_html))
            await asyncio.sleep(1.2)
            r = await cmd("Runtime.evaluate", expression=AUDIT, returnByValue=True)
            res = r.get("result", {}).get("value") or {}
            for p in res.get("problems", []):
                problems.append(f"{w}px: {p}")
            for cls, row in (res.get("rows") or {}).items():
                btns = row["btns"]
                if row["scroll"] > row["client"] + 1:
                    problems.append(f"{w}px: .{cls} overflows sideways "
                                    f"({row['scroll']} > {row['client']})")
                for b in btns:
                    # Tap size is a TOUCH concern. On the desktop modal these are .btn.small at 24px
                    # tall by design and driven by a mouse, so applying a finger-sized floor there
                    # just reports the stylesheet as a bug on all eight buttons at once.
                    if w < 800 and (b["w"] < MIN_TAP or b["h"] < MIN_TAP):
                        problems.append(f"{w}px: .{cls} {b['id']} is {b['w']}x{b['h']} — too small to tap")
                    if b["icon"] == 0:
                        problems.append(f"{w}px: .{cls} {b['id']} icon collapsed to 0x0")
                # The inline row's whole budget: Post stays on the FIRST line.
                if cls == "tl-cmp-tools" and btns:
                    top = min(b["y"] for b in btns)
                    post = [b for b in btns if b["id"] == "tl-cmp-post"]
                    if post and post[0]["y"] > top + 4:
                        problems.append(
                            f"{w}px: .{cls} Post wrapped onto its own line "
                            f"(y={post[0]['y']} vs row top {top}) — the row is over its budget; "
                            f"put the new control behind ⋯ instead")
                    used = max(b["x"] + b["w"] for b in btns) - min(b["x"] for b in btns)
                    print(f"  {w}px .{cls}: {len(btns)} buttons, {used}px used of {row['client']}px")
                else:
                    rows_used = len({b["y"] for b in btns})
                    print(f"  {w}px .{cls}: {len(btns)} buttons over {rows_used} line(s)")
    return problems


def main():
    if shutil.which("google-chrome-stable") is None:
        print("SKIP  no Chrome on this box")
        return 2
    import importlib.util
    if importlib.util.find_spec("websockets") is None:
        print("SKIP  websockets not installed")
        return 2

    src = open(APP, encoding="utf-8").read()
    inline = _extract(src, "tl-cmp-tools")
    modal = _extract(src, "cmp-tools")
    css = open(CSS, encoding="utf-8").read()
    sprite = open(SPRITE, encoding="utf-8").read()
    page = f"""<!doctype html><meta name=viewport content="width=device-width">
<style>{css}</style>
<body class="theme-cyberpunk">
<div class="tl-cmp">
  <!-- The avatar is LOAD-BEARING for this measurement: it is a sibling column that takes ~53px out
       of the body's width. Without it the row measures 332px at 360px instead of the real ~279px,
       which is enough slack to hide exactly the sixth-icon regression this check exists to catch. -->
  <img class="tl-cmp-av" src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==" alt="">
  <div class="tl-cmp-body">
  <textarea class="tl-cmp-ta" rows="1"></textarea>
  <div class="tl-cmp-tools">{inline}</div>
</div></div>
<div class="modal"><div class="modal-card"><div class="cmp-tools">{modal}</div></div></div>
<script>{sprite}</script>
"""
    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tab = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                tab = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                import time
                time.sleep(0.5)
        if not tab:
            print("SKIP  could not start Chrome")
            return 2
        problems = asyncio.run(drive(tab["webSocketDebuggerUrl"], page))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)

    if problems:
        print("\nCOMPOSER TOOLBAR REGRESSIONS")
        for p in problems:
            print("  -", p)
        return 1
    print("OK  composer toolbars fit at every width")
    return 0


if __name__ == "__main__":
    sys.exit(main())
