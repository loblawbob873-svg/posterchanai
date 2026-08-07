#!/usr/bin/env python3
"""The COMPOSE MODAL's footer, measured — the Post button must be on screen without scrolling.

    venv-unified/bin/python scripts/check_quote_modal.py

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome).

WHY: a quote post stacks a preview of the quoted note (`.cmp-ctx`, up to 230px) on top of a composer
that already wants ~220px of textarea plus two button rows. The whole sheet was ONE scroll box
(`.modal{overflow:auto}`), so the overflow came off the BOTTOM — the end you need. Shipped as "you
have to scroll down to see the buttons", and it is worst on a laptop (a 900px window leaves ~810px of
modal) and on a phone in landscape, i.e. the two places nobody checks.

check_composer_toolbar.py measures the tools ROW in isolation and cannot see this: it renders the row
in a bare div with no modal, no quote card and no viewport budget. This one renders the REAL modal
template (extracted from app.js, not retyped) inside a real `.modal-bg` at real viewport sizes and
asserts:

  * Post / Schedule are inside the modal's visible box with the sheet scrolled to the top;
  * so is the tools row (attach/emoji/⋯ — the controls you reach for while writing);
  * the textarea keeps a usable height (>= 90px) after the quote card takes its share;
  * the modal never exceeds the viewport, and never scrolls sideways;
  * a SHORT quote is not padded out to the shrink floor. That floor is a `min-height`, and a
    min-height is a minimum in ordinary layout too, not only under flex shrink — set a few px too
    high and every one-line quote gains a band of dead space above the box you type in. Which is
    the same complaint this file exists to fix, from the other direction.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")
SPRITE = os.path.join(ROOT, "static", "js", "client", "sprite.js")
PORT = 9475
PROFILE = "/tmp/pc-quote-modal-check"

# (width, height, devicePixelRatio). The dPR matters: client.css shrinks the whole app with
# `body{zoom}` between 821px and 1920px, but hands back zoom:1 to a hiDPI pointer-fine screen at
# >=1400px — so a retina laptop renders the composer at FULL size in a short window, which is the
# desktop case that actually broke. 844x390 is a modern phone, 780x360 the narrowest common one,
# 740x380 a phone held sideways.
# 1440x700 is the one that matters on a desktop: a window that is not maximised, or a laptop with a
# dock and a taskbar. A maximised 900px one happened to fit and hid this for as long as it existed.
VIEWS = [(1280, 900, 1), (1440, 900, 2), (1440, 700, 2), (1512, 860, 2), (1920, 1080, 1),
         (390, 844, 3), (360, 780, 3), (740, 380, 3)]
MIN_TA = 90
MAX_CTX_SLACK = 8   # px of unused height allowed inside the quoted card

# A long quoted note, so .cmp-ctx sits at its max-height rather than collapsing to one line.
QUOTED_BODY = ("the relay kept the event but the client never asked for it, which is the whole "
               "problem with treating an index as a source of truth. " * 4)
# …and its picture. _cmpCtx renders the original's media (a reply to a photo has to SHOW the photo),
# which is another ~130px of card, and it is the difference between "fits on a desktop" and does not.
# An SVG data URI so it has an intrinsic size with nothing to fetch.
QUOTED_IMG = ("data:image/svg+xml;utf8,"
              "%3Csvg xmlns='http://www.w3.org/2000/svg' width='800' height='600'%3E"
              "%3Crect width='800' height='600' fill='%23345'/%3E%3C/svg%3E")


def _resolve(html: str, keep_branches: bool) -> str:
    """Resolve `${cond ? '' : X}` template branches.

    Every one of them is gated on `quote`, so a quote post takes the EMPTY side (no poll, no
    background, no schedule) and a new post takes X. Repeated until stable because they nest.
    """
    pat = re.compile(r"\$\{[^{}]*?\?\s*''\s*:\s*([`'])(.*?)\1\s*\}", re.S)
    for _ in range(10):
        new = pat.sub((lambda m: m.group(2)) if keep_branches else "", html)
        if new == html:
            break
        html = new
    return html


def _template(src: str) -> str:
    """The literal passed to modal() inside compose() — the composer's real markup."""
    start = src.index("modal(`<h3>${title}</h3>${qhtml}")
    i = src.index("`", start) + 1
    j = src.index("`, root=>{", i)
    return src[i:j]


def _page(css: str, sprite: str, quote: bool, short: bool = False) -> str:
    body = _resolve(_template(open(APP, encoding="utf-8").read()), keep_branches=not quote)
    body = body.replace("${title}", "Quote post" if quote else "New post")
    ctx = ""
    if quote:
        txt = "gm" if short else QUOTED_BODY
        media = ("" if short else
                 f'<div class="media-row cmp-ctx-media"><img src="{QUOTED_IMG}" alt=""></div>')
        ctx = ('<div class="cmp-ctx"><div class="cmp-ctx-lbl">Quoting</div>'
               '<div class="quoted"><div class="hd"><img class="qav" alt="">'
               '<span class="name">satoshi</span><span class="time">2h</span></div>'
               f'<div class="txt">{txt}</div>{media}</div></div>')
    body = body.replace("${qhtml}", ctx)
    body = re.sub(r"\$\{.*?\}", "", body, flags=re.S)   # any leftover value interpolation
    return f"""<!doctype html><meta name=viewport content="width=device-width">
<style>{css}</style>
<body class="theme-cyberpunk modal-open">
<div id="modal-root"><div class="modal-bg"><div class="modal glass neon-border cmp-modal">{body}</div></div></div>
<script>{sprite}</script>
"""


AUDIT = r"""(() => {
  const m = document.querySelector('.modal');
  if (!m) return {problems: ['modal did not render']};

  const measure = () => {
    const mr = m.getBoundingClientRect();
    const o = {modal: {h: Math.round(mr.height)}, vh: innerHeight, items: {}};
    // The visible band of the sheet: inside the modal's own box AND inside the viewport.
    const visTop = Math.max(mr.top, 0), visBottom = Math.min(mr.bottom, innerHeight);
    for (const sel of ['#cmp', '#cmp-preview', '#cmp-send', '#cmp-sched-btn',
                       '#cmp-attach', '.cmp-tabs']) {
      const el = document.querySelector(sel);
      if (!el || el.classList.contains('hidden')) continue;
      const r = el.getBoundingClientRect();
      // FULLY inside, not merely intersecting: a Post button with its bottom half under the fold
      // is still a Post button you have to scroll for, and it is what "half a button" looks like.
      o.items[sel] = {top: Math.round(r.top), bottom: Math.round(r.bottom),
                      h: Math.round(r.height), w: Math.round(r.width),
                      hidden: r.bottom > visBottom + 1 || r.top < visTop - 1};
    }
    return o;
  };

  const out = Object.assign({problems: []}, measure());
  // Dead space inside the quoted card: how much taller its box is than the content it holds. A
  // shrink floor that is too high shows up here and nowhere else — the layout still "fits".
  const q = document.querySelector('.cmp-ctx .quoted');
  if (q) out.ctxSlack = Math.round(q.clientHeight - q.scrollHeight);
  out.hscroll = document.documentElement.scrollWidth > innerWidth + 1;

  // The textarea's resize grip must still work. It writes an inline `height`, and a flex-basis
  // LENGTH would win over that and freeze the drag — which is why the rule sets `height` and lets
  // the basis stay auto. Nothing else would notice the difference, so assert it directly.
  // Measured through getComputedStyle, NOT getBoundingClientRect: client.css scales the desktop app
  // with `body{zoom}`, and a rect is in ZOOMED px while an inline `height` is in CSS px — mixing the
  // two writes a height smaller than the one already there and reports a working grip as broken.
  const ta = document.querySelector('#cmp');
  if (ta) {
    const before = parseFloat(getComputedStyle(ta).height);
    ta.style.height = Math.round(before + 60) + 'px';
    out.resizeDelta = Math.round(parseFloat(getComputedStyle(ta).height) - before);
    ta.style.height = '';
    out.modal.capped = m.scrollHeight > m.clientHeight + 1 ||
                       m.getBoundingClientRect().height + 40 >= innerHeight * 0.92;
  }

  // Second state of the same sheet: the Preview tab, which swaps a bounded textarea for a pane
  // sized by its CONTENT. A long post is exactly the case that would push Post off again.
  const pv = document.querySelector('#cmp-preview');
  if (ta && pv) {
    pv.innerHTML = '<div class="txt">' + 'a very long previewed post. '.repeat(120) + '</div>';
    ta.classList.add('hidden');
    pv.classList.remove('hidden');
    out.preview = measure();
  }
  return out;
})()"""


async def drive(ws_url, pages):
    import websockets
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
        for label, html in pages:
            for w, h, dpr in VIEWS:
                await cmd("Emulation.setDeviceMetricsOverride", width=w, height=h,
                          deviceScaleFactor=dpr, mobile=w < 800)
                await cmd("Page.navigate", url="data:text/html;charset=utf-8," +
                          urllib.parse.quote(html))
                await asyncio.sleep(1.0)
                r = await cmd("Runtime.evaluate", expression=AUDIT, returnByValue=True)
                res = r.get("result", {}).get("value") or {}
                tag = f"{label} {w}x{h}"
                for p in res.get("problems", []):
                    problems.append(f"{tag}: {p}")
                if not res.get("items"):
                    continue
                items = res["items"]
                for state, blk in (("", res), ("preview tab: ", res.get("preview") or {})):
                    for sel, it in (blk.get("items") or {}).items():
                        if it["hidden"]:
                            problems.append(
                                f"{tag}: {state}{sel} is off the visible sheet "
                                f"(top={it['top']} bottom={it['bottom']} vh={res['vh']}) "
                                f"— you have to scroll to reach it")
                # Only where the sheet has room to grow into. Once it is at its max height there is
                # nowhere for a drag to go, and shrinking it back is the correct behaviour.
                if not res["modal"].get("capped") and res.get("resizeDelta", 60) < 55:
                    problems.append(f"{tag}: the textarea's resize grip is dead — a +60px drag "
                                    f"moved it {res['resizeDelta']}px (a flex-basis LENGTH on "
                                    f"#cmp beats the inline height a drag writes; use `height`)")
                ta = items.get("#cmp")
                if ta and ta["h"] < MIN_TA:
                    problems.append(f"{tag}: textarea squeezed to {ta['h']}px "
                                    f"(min {MIN_TA}) — nothing left to type in")
                if res["modal"]["h"] > res["vh"] + 1:
                    problems.append(f"{tag}: modal is {res['modal']['h']}px tall "
                                    f"in a {res['vh']}px viewport")
                if res["hscroll"]:
                    problems.append(f"{tag}: page scrolls sideways")
                if label == "quote-short" and res.get("ctxSlack", 0) > MAX_CTX_SLACK:
                    problems.append(f"{tag}: a one-line quote is padded out by "
                                    f"{res['ctxSlack']}px of dead space — the shrink floor on "
                                    f".cmp-ctx .quoted is above its natural height")
                send = items.get("#cmp-send", {})
                print(f"  {tag}: modal {res['modal']['h']}px, textarea "
                      f"{(ta or {}).get('h', '-')}px, Post bottom {send.get('bottom', '-')} "
                      f"of {res['vh']}px, quote slack {res.get('ctxSlack', '-')}")
    return problems


def main():
    if shutil.which("google-chrome-stable") is None:
        print("SKIP  no Chrome on this box")
        return 2
    import importlib.util
    if importlib.util.find_spec("websockets") is None:
        print("SKIP  websockets not installed")
        return 2

    css = open(CSS, encoding="utf-8").read()
    sprite = open(SPRITE, encoding="utf-8").read()
    pages = [("quote", _page(css, sprite, True)),
             ("quote-short", _page(css, sprite, True, short=True)),
             ("new", _page(css, sprite, False))]

    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        # Headless Chrome reports `hover:none` / `pointer:coarse` — it has no real pointer — so the
        # `zoom:1` desktop tier in client.css (min-width:1400 + hiDPI + hover + fine) NEVER matched
        # and every "desktop" size was silently measured at zoom .67-.77, i.e. two thirds size: the
        # easy case, and not the one people are on. Force a mouse. Nothing else in the stylesheet
        # keys layout off hover/pointer (only :hover paint), so this cannot skew the phone sizes.
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
         "--blink-settings=primaryHoverType=2,availableHoverTypes=2,"
         "primaryPointerType=4,availablePointerTypes=4",
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
                time.sleep(0.5)
        if not tab:
            print("SKIP  could not start Chrome")
            return 2
        problems = asyncio.run(drive(tab["webSocketDebuggerUrl"], pages))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)

    if problems:
        print("\nCOMPOSE MODAL REGRESSIONS")
        for p in problems:
            print("  -", p)
        return 1
    print("OK  compose modal fits — Post is reachable without scrolling at every size")
    return 0


if __name__ == "__main__":
    sys.exit(main())
