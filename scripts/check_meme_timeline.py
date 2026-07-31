#!/usr/bin/env python3
"""Meme Builder timeline: a clip's bar must depend on that clip ALONE.

Run: venv-unified/bin/python scripts/check_meme_timeline.py

The lane used to be stretched to exactly the project length, so a second was worth however much room
happened to be left over. Change ONE clip's length and every OTHER bar moved and resized — reported as
"it needs to stop changing layers when I adjust the length of another". The fix is a fixed pixels-per-
second scale (--mb-pps) with the lane pinned to --mb-lane-w instead of flex:1 1 auto.

That fix lives half in meme.js (the inline width it emits) and half in client.css (the custom properties
and the pinned lane), so neither file can be checked on its own — this renders the real stylesheet in
headless Chrome and MEASURES the boxes:

  * a clip's bar keeps its exact pixel position and width when a DIFFERENT clip is re-timed;
  * the same clip draws identically in a wide panel and a narrow one (the scale is not the port's width);
  * a ruler tick sits exactly over the clip that starts at that second (they share --mb-name-w, and a
    breakpoint that moved only the label column would slide them apart);
  * the rows still reach across a wide panel (min-width:100%) without that width becoming more seconds.

Mirrors the geometry meme.js emits — keep the two in step if timelineInner()/trackEl() change.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "css", "client.css")
MEME_JS = os.path.join(ROOT, "static", "js", "client", "meme.js")

TL_MIN_SPAN, TL_TAIL = 8, 2


def _chrome():
    for b in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(b)
        if p:
            return p
    return None


def _span(clips):
    """tlSpan() — must match meme.js."""
    return max(TL_MIN_SPAN, max((c["start"] + c["dur"] for c in clips), default=0) + TL_TAIL)


def _page(clips, panel_px, zoom=1):
    """The markup timelineInner()/trackEl() produce, against the real stylesheet."""
    span = _span(clips)
    lane = f"calc(var(--mb-pps) * {span * zoom:.3f})"
    rows = []
    for i, c in enumerate(clips):
        left, wid = c["start"] / span * 100, max(3.0, c["dur"] / span * 100)
        rows.append(
            f'<div class="mb-track" data-id="c{i}"><div class="mb-trackname"></div>'
            f'<div class="mb-lane"><div class="mb-clip" id="clip{i}" '
            f'style="left:{left:.3f}%;width:{wid:.3f}%"></div></div></div>')
    ticks = "".join(
        f'<i class="mb-tick" id="tick{t}" style="left:{t / span * 100:.3f}%"><b>{t}s</b></i>'
        for t in range(0, int(span) + 1))
    return f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="file://{CSS}">
<body><div class="mb-timeline" style="width:{panel_px}px">
<div class="mb-tlinner" id="mb-tlinner" style="--mb-lane-w:{lane};width:calc(var(--mb-name-w) + {lane})">
<div class="mb-track mb-rrow"><div class="mb-trackname mb-rname"></div>
<div class="mb-lane mb-rlane" id="rlane">{ticks}</div></div>
{''.join(rows)}
</div></div>
<script>
window.__RESULT = (function(){{
  const out = {{clips: [], ticks: {{}}, inner: 0, port: 0}};
  const portEl = document.querySelector('.mb-timeline');
  const port = portEl.getBoundingClientRect();
  const cs = getComputedStyle(portEl);
  // The CONTENT box: .mb-timeline is padded, and min-width:100% on the inner resolves against the
  // content box, not the border box. Comparing against the padded width fails by exactly the padding.
  out.port = port.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  out.inner = document.getElementById('mb-tlinner').getBoundingClientRect().width;
  document.querySelectorAll('.mb-clip').forEach(function(el){{
    const r = el.getBoundingClientRect();
    out.clips.push({{id: el.id, left: r.left - port.left, width: r.width}});
  }});
  document.querySelectorAll('.mb-tick').forEach(function(el){{
    out.ticks[el.id] = el.getBoundingClientRect().left - port.left;
  }});
  return out;
}})();
document.title = JSON.stringify(window.__RESULT);
</script></body>"""


def _measure(chrome, clips, panel_px, width=1280, zoom=1):
    with tempfile.TemporaryDirectory() as td:
        page = os.path.join(td, "p.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(_page(clips, panel_px, zoom))
        out = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox", "--allow-file-access-from-files",
             f"--window-size={width},900", "--virtual-time-budget=1500", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=120).stdout
        m = re.search(r"<title>(.*?)</title>", out, re.S)
        if not m:
            raise SystemExit("FAIL  the page did not render (no measurement in the DOM)")
        return json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))


def main():
    chrome = _chrome()
    if not chrome:
        print("SKIP  no headless Chrome on this node")
        return 0

    # The source of truth for the constants, so a change in meme.js that this file does not follow is loud.
    src = open(MEME_JS, encoding="utf-8").read()
    m = re.search(r"const TL_MIN_SPAN = (\d+), TL_TAIL = (\d+);", src)
    if not m or (int(m.group(1)), int(m.group(2))) != (TL_MIN_SPAN, TL_TAIL):
        print(f"FAIL  meme.js's TL_MIN_SPAN/TL_TAIL no longer match this check ({m and m.groups()})")
        return 1

    fails = []

    # 1. Re-timing clip 3 must not move clips 1 and 2 by a single pixel.
    before = [{"start": 0, "dur": 3}, {"start": 3, "dur": 2}, {"start": 5, "dur": 4}]
    after = [{"start": 0, "dur": 3}, {"start": 3, "dur": 2}, {"start": 5, "dur": 12}]
    a = _measure(chrome, before, 900)
    b = _measure(chrome, after, 900)
    for i in (0, 1):
        da = abs(a["clips"][i]["left"] - b["clips"][i]["left"])
        dw = abs(a["clips"][i]["width"] - b["clips"][i]["width"])
        if da > 0.5 or dw > 0.5:
            fails.append(f"clip{i} moved when a DIFFERENT clip was re-timed "
                         f"(left {a['clips'][i]['left']:.1f}->{b['clips'][i]['left']:.1f}, "
                         f"width {a['clips'][i]['width']:.1f}->{b['clips'][i]['width']:.1f})")

    # 2. The scale is not the panel's width: the same project draws the same bars in a narrow panel.
    wide = _measure(chrome, before, 1100)
    narrow = _measure(chrome, before, 620)
    for i in range(len(before)):
        if abs(wide["clips"][i]["width"] - narrow["clips"][i]["width"]) > 0.5:
            fails.append(f"clip{i} changed width with the PANEL width "
                         f"({wide['clips'][i]['width']:.1f} vs {narrow['clips'][i]['width']:.1f})")

    # 3. A tick sits over the clip that starts at that second (shared --mb-name-w).
    if abs(a["ticks"]["tick3"] - a["clips"][1]["left"]) > 1.0:
        fails.append(f"the 3s tick ({a['ticks']['tick3']:.1f}px) is not over the clip that starts at 3s "
                     f"({a['clips'][1]['left']:.1f}px)")

    # 4. Rows still reach across a wide panel — min-width:100%, without it becoming more seconds.
    if wide["inner"] < wide["port"] - 1:
        fails.append(f"the timeline does not span the panel ({wide['inner']:.0f} < {wide['port']:.0f})")

    # 5. Same, on a phone: the narrow breakpoint changes --mb-pps, so bars scale together but a
    #    re-timed clip still must not move its neighbours.
    pa = _measure(chrome, before, 360, width=390)
    pb = _measure(chrome, after, 360, width=390)
    for i in (0, 1):
        if abs(pa["clips"][i]["left"] - pb["clips"][i]["left"]) > 0.5:
            fails.append(f"phone: clip{i} moved when a different clip was re-timed")

    if fails:
        print("FAIL  meme timeline geometry:")
        for f in fails:
            print("   -", f)
        return 1
    print(f"timeline: clip px stable across re-timing, panel width and the phone breakpoint "
          f"(1x lane {a['inner']:.0f}px)")
    print("OK  meme timeline checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
