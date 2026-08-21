#!/usr/bin/env python3
"""Does the terminal actually re-fit going desktop → phone → desktop?

    venv-unified/bin/python scripts/check_terminal_resize.py

Asked as "will the terminal resize properly going from desktop to phone and back?", and it is not a
question the layout check next door can answer: `scripts/check_terminal_mobile.py` STUBS the emulator
(a real xterm needs a canvas, and what that check measures is the chrome around it). The number that
decides whether a shell is readable is `term.cols`, and only the real FitAddon against the real
stylesheet produces it.

So this loads the VENDORED xterm + fit addon in headless Chrome, mounts them the way term.js does,
and measures the grid at three sizes in one page: a desktop window, a phone-shaped box, and back to
the desktop. What it asserts is what actually goes wrong:

  * a fit that returns a 1-row or 2-column grid — the shape a hidden or zero-height box produces, and
    the reason `_fit` refuses to send those. A shell told it has one row scrolls every line away.
  * a phone that gets fewer than ~30 columns, where `ls -l` no longer lines up and anything drawing a
    box is unreadable. This is what the font-size tiers in `fontSize()` exist to prevent, so the tiers
    are read out of term.js and applied here rather than guessed.
  * a return to the desktop that does NOT come back to the original grid — the "and back" half. A
    session survives being picked up on a phone and put down again, so a terminal that only shrinks
    leaves a 200-column shell rendering into 40 columns for the rest of its life.

Exit 0 = the grid is sane at both ends and returns; 1 = it is not; 2 = could not run.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = (shutil.which("chromium") or shutil.which("chromium-browser")
          or shutil.which("google-chrome") or shutil.which("google-chrome-stable"))

# (label, viewport width, height). The desktop one is deliberately wide: a 200-column shell is the
# case the phone has to shrink away from and then give back.
SIZES = [("desktop", 1440, 900), ("tablet", 1024, 768)]
PHONE_W, PHONE_H = 390, 720


def _tiers():
    """`fontSize()`'s thresholds, read out of the shipped source rather than restated here — a copy
    would keep passing after somebody changed the real one."""
    src = open(os.path.join(ROOT, "static", "js", "client", "term.js"), encoding="utf-8").read()
    m = re.search(r"function fontSize\(\)\{(.*?)\n    \}", src, re.S)
    if not m:
        print("FAIL  could not find fontSize() in term.js — re-point this check")
        sys.exit(2)
    # PAGEZOOM COMES WITH IT. fontSize multiplies by the page's scale, because the terminal host
    # undoes body{zoom} and the font must be scaled by the same factor. Lifting only the tiers gave
    # "pageZoom is not defined" -- and a check that cannot run is not a check that passes.
    z = re.search(r"function pageZoom\(\)\{(.*?)\n    \}", src, re.S)
    if not z:
        print("FAIL  could not find pageZoom() in term.js — re-point this check")
        sys.exit(2)
    return "function pageZoom(){" + z.group(1) + "\n    }\n" + m.group(1)


PAGE = """<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="%(xterm)s">
<link rel="stylesheet" href="%(css)s">
<style>html,body{margin:0;height:100%%}
  .app{display:flex;flex-direction:column;height:100dvh}
  #feed{flex:1;min-height:0;display:flex;flex-direction:column}</style>
<div class="app"><div id="feed" class="feed feed-term">
  <div class="tty-wrap">
    <div class="tty-bar"><select class="input tty-host" id="tty-host"></select>
      <button class="btn btn-neon small" id="tty-go">Connect</button>
      <span class="tty-state" id="tty-state">connected</span></div>
    <div class="tty-screen"><div class="tty-fit" id="tty-screen"></div></div>
    <div class="tty-keys" id="tty-keys"><button data-k="Escape">esc</button>
      <button data-k="Tab">tab</button><button data-k="ctrl">ctrl</button>
      <button data-k="ArrowUp">↑</button><button data-k="ArrowDown">↓</button>
      <button data-k="^C">^C</button><button data-k="kbd">⌨</button></div>
    <input class="tty-catch" id="tty-catch">
  </div></div></div>
<script src="%(xtermjs)s"></script>
<script src="%(fitjs)s"></script>
<script>
(function(){
  const out = { steps: [] };
  // term.js's own tiering, verbatim (see _tiers()).
  function fontSize(){
    %(tiers)s
  }
  try{
    const term = new window.Terminal({ fontSize: fontSize(), cursorBlink:false, scrollback:1000,
      fontFamily:'ui-monospace, Menlo, Consolas, "DejaVu Sans Mono", monospace' });
    const F = window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon);
    const fit = F ? new F() : null;
    if(fit) term.loadAddon(fit);
    term.open(document.getElementById('tty-screen'));
    const wrap = document.querySelector('.tty-wrap');
    const step = (label) => {
      try{ term.options.fontSize = fontSize(); }catch(_){}
      try{ if(fit) fit.fit(); }catch(_){}
      out.steps.push({ label, cols: term.cols|0, rows: term.rows|0,
                       font: term.options.fontSize,
                       w: Math.round(wrap.getBoundingClientRect().width) });
    };
    step('open');
    /* SHRINK THE BOX, not the window — headless Chrome cannot be resized mid-run, and the element is
     * what the ResizeObserver in term.js watches anyway, so this is the same input the real resize
     * path receives. */
    const app = document.querySelector('.app');
    // BOTH AXES. Width is what decides whether `ls -l` lines up; ROWS is the axis a phone's on-screen
    // keyboard halves the moment you type, and an explicit height on `.tty-wrap` does nothing —
    // it is a flex item, so its basis wins. The height has to be constrained at the container.
    // HALF the current viewport height, not a fixed number: a soft keyboard takes roughly half the
    // screen, and a fixed 720px is not shorter than a 768px tablet at all — which made this step
    // measure a font-tier change rather than a resize.
    wrap.style.width = %(pw)d + 'px';
    app.style.height = Math.round(window.innerHeight * 0.5) + 'px';
    step('phone');
    wrap.style.width = ''; app.style.height = '';
    step('back');
    out.ok = true;
  }catch(e){ out.ok = false; out.err = String(e && e.message || e); }
  document.title = JSON.stringify(out);
})();
</script>"""


def main():
    if not CHROME:
        print("SKIP  no chrome on this node")
        return 0
    tmp = tempfile.mkdtemp(prefix="pcresize-")
    problems = []
    page = PAGE % {
        "css": "file://" + os.path.join(ROOT, "static", "css", "client.css"),
        "xterm": "file://" + os.path.join(ROOT, "static", "vendor", "xterm", "xterm.css"),
        "xtermjs": "file://" + os.path.join(ROOT, "static", "vendor", "xterm", "xterm.js"),
        "fitjs": "file://" + os.path.join(ROOT, "static", "vendor", "xterm", "fit.js"),
        "tiers": _tiers(),
        "pw": PHONE_W, "ph": PHONE_H,
    }
    path = os.path.join(tmp, "r.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)

    try:
        for label, w, h in SIZES:
            res = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                 f"--window-size={w},{h}", "--virtual-time-budget=6000", "--dump-dom",
                 "--allow-file-access-from-files", "file://" + path],
                capture_output=True, text=True, timeout=120).stdout
            m = re.search(r"<title>(.*?)</title>", res, re.S)
            if not m:
                problems.append((label, "no-output", "the page produced no measurements"))
                continue
            q = json.loads(m.group(1).replace("&quot;", '"'))
            if not q.get("ok"):
                problems.append((label, "threw", q.get("err") or "the emulator did not mount"))
                continue
            steps = {s["label"]: s for s in q["steps"]}
            print(f"{label} {w}px: " + "  ".join(
                f"{k}={s['cols']}x{s['rows']}@{s['font']}px" for k, s in steps.items()))

            for k, s in steps.items():
                if s["cols"] < 2 or s["rows"] < 2:
                    problems.append((label, "degenerate", f"the {k} fit produced a "
                                                          f"{s['cols']}x{s['rows']} grid"))
            ph = steps.get("phone") or {}
            if ph and ph.get("cols", 0) < 30:
                problems.append((label, "phone-narrow",
                                 f"a phone-width box fits only {ph['cols']} columns — `ls -l` does "
                                 "not line up and anything drawing a box is unreadable"))
            op, bk = steps.get("open") or {}, steps.get("back") or {}
            if op and ph and ph.get("cols", 0) >= op.get("cols", 0):
                problems.append((label, "no-shrink", "the grid did not shrink for a phone-sized box"))
            if op and ph and ph.get("rows", 0) >= op.get("rows", 0):
                problems.append((label, "no-shrink-rows",
                                 "the row count did not change for a shorter viewport — the axis a "
                                 "soft keyboard halves"))
            if op and bk and (op["cols"], op["rows"]) != (bk["cols"], bk["rows"]):
                problems.append((label, "no-return",
                                 f"back at {label} the grid is {bk['cols']}x{bk['rows']}, not the "
                                 f"{op['cols']}x{op['rows']} it started at — a session picked up on "
                                 "a phone would stay phone-shaped"))
    except subprocess.TimeoutExpired:
        print("FAIL  chrome timed out")
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        print()
        for where, kind, why in problems:
            print(f"FAIL  [{where}] {kind}: {why}")
        return 1
    print("OK  the terminal re-fits desktop → phone → desktop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
