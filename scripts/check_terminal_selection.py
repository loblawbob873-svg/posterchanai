#!/usr/bin/env python3
"""Does a click in the terminal land on the line you clicked?

    venv-unified/bin/python scripts/check_terminal_selection.py

Reported twice, in the same words both times: "terminal does not highlight the line I want, it is a
few lines above it". The cause is not xterm and not the stylesheet on its own -- it is the two of
them disagreeing about what a pixel is.

Desktop scales the whole page with `body{zoom:.67-.77}`. xterm hit-tests a click by taking
`clientY - getBoundingClientRect().top` -- both VISUAL pixels under zoom -- and dividing by the cell
height it computed from the font, which is a LAYOUT pixel. Measured before the fix:

    tier      zoom   rendered row   xterm's cell
    1366      0.67      10.72px        16px
    1600      0.72      11.52px        16px
    1920      0.77      12.32px        16px
    phone     1.00      16px           16px

They agree at zoom 1 and diverge by exactly the zoom factor otherwise, so a click ten rows down is
divided by 16 when each row occupies 10.72 and lands on row six -- further off the lower you click,
which is why it reads as "a few lines".

THIS MEASURES THE TWO NUMBERS RATHER THAN THE SYMPTOM. Simulated mouse events do not produce a
selection in headless Chrome (xterm's selection service wants a real pointer sequence), so asserting
"the right text was highlighted" would be asserting that the harness works. The RATIO is the fault,
it is exact, and it is what regressed.

Exit 0 = every tier agrees; 1 = a tier does not; 2 = could not run.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = (shutil.which("chromium") or shutil.which("chrome")
          or shutil.which("google-chrome") or shutil.which("google-chrome-stable"))

# The zoom tiers the stylesheet actually defines, plus a phone where there is no zoom at all — the
# control that proves the check can tell agreement from coincidence.
SIZES = [("desktop-1366", 1366, 768), ("desktop-1600", 1600, 900),
         ("wide-1920", 1920, 1080), ("phone-420", 420, 860)]

PAGE = """<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="%(xterm)s">
<link rel="stylesheet" href="%(css)s">
<style>html,body{margin:0;height:100%%}
  .app{display:flex;flex-direction:column;height:100dvh}
  #feed{flex:1;min-height:0;display:flex;flex-direction:column}</style>
<div class="app"><div id="feed" class="feed feed-term">
  <div class="tty-wrap">
    <div class="tty-screen"><div class="tty-fit" id="tty-screen"></div></div>
  </div></div></div>
<script src="%(xtermjs)s"></script>
<script src="%(fitjs)s"></script>
<script>
(function(){
  const out = {};
  try{
    const ZF = parseFloat(getComputedStyle(document.body).zoom) || 1;
    %(tiers)s
    const term = new window.Terminal({ fontSize: fontSize(), cursorBlink:false, scrollback:1000,
      fontFamily:'ui-monospace, Menlo, Consolas, "DejaVu Sans Mono", monospace' });
    const F = window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon);
    const fit = F ? new F() : null; if(fit) term.loadAddon(fit);
    const box = document.getElementById('tty-screen');
    term.open(box); if(fit) fit.fit();
    for(let i=0;i<term.rows;i++) term.writeln('LINE' + String(i).padStart(3,'0'));
    const screenEl = box.querySelector('.xterm-screen') || box.querySelector('.xterm');
    const r = screenEl.getBoundingClientRect();
    out.zoom = ZF;
    out.rows = term.rows|0;
    out.cols = term.cols|0;
    // What one row ACTUALLY occupies where a pointer lands.
    out.rendered = +(r.height / term.rows).toFixed(3);
    // What xterm divides a click by.
    const d = term._core && term._core._renderService && term._core._renderService.dimensions;
    out.cell = d && d.css && d.css.cell ? +d.css.cell.height
             : (d && d.actualCellHeight != null ? +d.actualCellHeight : null);
    out.ok = true;
  }catch(e){ out.ok = false; out.err = String(e && e.message || e); }
  document.title = JSON.stringify(out);
})();
</script>"""


def _fn(name):
    """A function lifted out of the shipped term.js, so this cannot pass against a copy that has
    drifted from the real one."""
    src = open(os.path.join(ROOT, "static", "js", "client", "term.js"), encoding="utf-8").read()
    m = re.search(r"function %s\(\)\{(.*?)\n    \}" % name, src, re.S)
    if not m:
        print("FAIL  could not find %s() in term.js — re-point this check" % name)
        sys.exit(2)
    return "function %s(){%s\n    }" % (name, m.group(1))


def main():
    if not CHROME:
        print("SKIP  no chrome on this node")
        return 0
    for f in ("xterm/xterm.js", "xterm/fit.js", "xterm/xterm.css"):
        if not os.path.isfile(os.path.join(ROOT, "static", "vendor", f)):
            print("SKIP  vendored xterm is not here (%s)" % f)
            return 0

    tmp = tempfile.mkdtemp(prefix="pcsel-")
    page = PAGE % {
        "css": "file://" + os.path.join(ROOT, "static", "css", "client.css"),
        "xterm": "file://" + os.path.join(ROOT, "static", "vendor", "xterm", "xterm.css"),
        "xtermjs": "file://" + os.path.join(ROOT, "static", "vendor", "xterm", "xterm.js"),
        "fitjs": "file://" + os.path.join(ROOT, "static", "vendor", "xterm", "fit.js"),
        "tiers": _fn("pageZoom") + "\n    " + _fn("fontSize"),
    }
    path = os.path.join(tmp, "s.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)

    problems, seen = [], []
    try:
        for label, w, h in SIZES:
            res = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                 f"--window-size={w},{h}", "--virtual-time-budget=8000", "--dump-dom",
                 "--allow-file-access-from-files", "file://" + path],
                capture_output=True, text=True, timeout=120).stdout
            m = re.search(r"<title>(.*?)</title>", res, re.S)
            if not m:
                problems.append((label, "no-output", "the page produced no measurements"))
                continue
            q = json.loads(m.group(1).replace("&quot;", '"'))
            if not q.get("ok"):
                problems.append((label, "threw", q.get("err", "?")))
                continue
            cell, rendered = q.get("cell"), q.get("rendered")
            print("%-14s zoom=%-5s %3dx%-3d  rendered row=%-7s xterm cell=%s"
                  % (label, q["zoom"], q["cols"], q["rows"], rendered, cell))
            seen.append((label, q))
            if not cell:
                problems.append((label, "no-cell", "xterm did not report a cell height"))
                continue
            # A HALF PIXEL, not an exact match: the rendered height is a rect divided by a row count
            # and lands on fractions. The fault this exists for is a factor of 0.67, not a rounding.
            if abs(rendered - cell) > 0.5:
                problems.append((label, "click-lands-elsewhere",
                                 "a row occupies %.2fpx and xterm divides clicks by %.2fpx — "
                                 "a click %d rows down lands on row %d"
                                 % (rendered, cell, 10, round(10 * rendered / cell))))
    except subprocess.TimeoutExpired:
        print("FAIL  chrome timed out")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # THE CONTROL: the phone tier has no zoom, so if it disagreed the fault would be somewhere else
    # entirely and every "pass" above would be meaningless.
    if not any(l.startswith("phone") for l, _ in seen):
        print("FAIL  the unzoomed control never ran — a pass here would prove nothing")
        return 1

    if problems:
        for label, why, detail in problems:
            print("FAIL  [%s] %s: %s" % (label, why, detail))
        return 1
    print("OK  a click lands on the row it was made on, at every zoom tier")
    return 0


if __name__ == "__main__":
    sys.exit(main())
