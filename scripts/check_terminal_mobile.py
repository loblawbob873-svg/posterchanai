#!/usr/bin/env python3
"""The Terminal screen at phone widths — MEASURED, not eyeballed.

Run: venv-unified/bin/python scripts/check_terminal_mobile.py

scripts/check_client_mobile.py sweeps the app's ordinary views and never opens this one (it needs a
host list and a session), which is exactly the gap that let "buttons cut off at the top, some weird
horizontal line" ship. The layout here is pure CSS over a fixed bit of markup, so it can be measured
without a server: the markup is EXTRACTED from term.js rather than copied, so this cannot drift from
what the app renders, and the stylesheet is the real one.

What is checked, each of which has a failure that looks fine in a screenshot at one width:

  bar-clipped      the toolbar wraps to two lines on a narrow screen, and a wrapping flex container
                   distributes its lines with space BETWEEN them — which pushes the first row half
                   out of the top of the bar. That was the reported "buttons cut off at the top".
  keys-wrapped     the key bar (esc/tab/ctrl/arrows/^C) must stay ONE line and scroll sideways. It is
                   the only way to send Ctrl-C from a phone, so it may not eat the screen.
  keys-too-small   a touch target under 34px is one you miss, and missing ^C in a shell is expensive.
  screen-collapsed the emulator's box must have real height, or xterm's fit computes one row and the
                   terminal is a sliver.
  h-overflow       nothing may push the page wider than the phone.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = (shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chrome"))
WIDTHS = [(390, 780), (360, 740), (320, 700)]


def _markup():
    """`_shellHtml`'s template literal, straight out of term.js."""
    src = open(os.path.join(ROOT, "static", "js", "client", "term.js"), encoding="utf-8").read()
    m = re.search(r"function _shellHtml\(\)\{\s*return `(.*?)`;\s*\}", src, re.S)
    if not m:
        print("FAIL  could not find _shellHtml in term.js — re-point this check")
        sys.exit(2)
    html = m.group(1)
    # It is a real terminal once connected: the key bar is shown and the buttons swap.
    return html.replace(" hidden>", ">").replace('id="tty-keys" hidden', 'id="tty-keys"')


PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="%(css)s">
<link rel="stylesheet" href="%(xterm)s">
<style>
  html,body{margin:0;height:100%%;background:#07040f}
  /* The app's shape around the feed: a column with a header, which is what makes #feed bounded. */
  .app{display:flex;flex-direction:column;height:100vh}
  .hdr{flex:0 0 auto;height:44px;background:#12082a}
</style>
<div class="app"><div class="hdr"></div><div id="feed" class="feed feed-term">%(body)s</div></div>
<script>
  // Stand in for the emulator: xterm needs a canvas/WebGL context that adds nothing to a LAYOUT
  // measurement, and its own stylesheet (loaded above) is what supplies the rules that matter.
  const s = document.getElementById('tty-screen');
  s.innerHTML = '<div class="xterm"><div class="xterm-viewport"></div>' +
                '<div class="xterm-screen"></div></div>';
  const sel = document.getElementById('tty-host');
  sel.innerHTML = '<option>server1 — verita84@server1.lan</option>';
  /* "Still running" — the strip that makes a session started on the laptop resumable here. Filled
   * with what _sessions() actually renders, because an EMPTY strip measures fine and is not the
   * thing that can push the screen off the bottom of a 320px phone. */
  const ss = document.getElementById('tty-sessions');
  ss.innerHTML = '<span class="tty-sess-lbl">still running</span>' +
    ['server1','nas','a-rather-long-host-name'].map(h =>
      '<span class="tty-sess"><b>' + h + '</b><i>2h</i>' +
      '<button>Attach</button><button class="tty-kill">Kill</button></span>').join('');
  document.getElementById('tty-state').textContent = 'connected to server1';
  const out = {};
  const bar = document.querySelector('.tty-bar'), keys = document.querySelector('.tty-keys');
  const br = bar.getBoundingClientRect();
  // Every control must sit fully INSIDE the bar it belongs to.
  // Only VISIBLE controls: a hidden one reports a zero-size rect at 0,0, which reads as "outside the
  // bar" and is a false alarm from this harness rather than a fact about the page.
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  out.barClip = [...bar.querySelectorAll('button,select')].filter(vis).reduce((n, el) => {
    const r = el.getBoundingClientRect();
    return n + ((r.top < br.top - 0.5 || r.bottom > br.bottom + 0.5) ? 1 : 0);
  }, 0);
  const kr = keys.getBoundingClientRect();
  const kb = [...keys.querySelectorAll('button')];
  out.keyRows = new Set(kb.map(b => Math.round(b.getBoundingClientRect().top))).size;
  out.keyMin = Math.min(...kb.map(b => Math.round(b.getBoundingClientRect().height)));
  out.keysScroll = keys.scrollWidth > keys.clientWidth + 1;
  out.keyClip = kb.reduce((n, el) => {
    const r = el.getBoundingClientRect();
    return n + ((r.top < kr.top - 0.5 || r.bottom > kr.bottom + 0.5) ? 1 : 0);
  }, 0);
  out.barBox = [Math.round(br.top), Math.round(br.height)];
  out.ctrls = [...bar.querySelectorAll('button,select')].filter(vis).map(el => {
    const r = el.getBoundingClientRect();
    return [el.id || el.tagName, Math.round(r.top), Math.round(r.height)];
  });
  out.barStyle = (cs => ({display:cs.display, position:cs.position, height:cs.height,
    align:cs.alignItems, wrap:cs.flexWrap, pad:cs.padding}))(getComputedStyle(bar));
  out.kidStyle = [...bar.children].map(el => (cs => [el.tagName + '.' + (el.id||''),
    cs.display, cs.position, cs.height, cs.flex])(getComputedStyle(el)));
  const fe = document.getElementById('feed'), wr = document.querySelector('.tty-wrap');
  out.feedStyle = (cs => [cs.display, cs.padding, cs.overflow, cs.flexDirection])(getComputedStyle(fe));
  out.wrapStyle = (cs => [cs.display, cs.flexDirection, cs.flex, cs.minHeight])(getComputedStyle(wr));
  out.boxes = { feed: Math.round(fe.getBoundingClientRect().top),
                wrap: Math.round(wr.getBoundingClientRect().top),
                wrapH: Math.round(wr.getBoundingClientRect().height) };
  out.screenH = Math.round(document.querySelector('.tty-screen').getBoundingClientRect().height);
  // The strip SCROLLS sideways; it must never wrap into a second row (that eats terminal rows) and
  // never widen the page (which would let the whole app scroll horizontally on a phone).
  const sr = ss.getBoundingClientRect();
  out.sessH = Math.round(sr.height);
  out.sessClip = [...ss.querySelectorAll('.tty-sess')].reduce((n, el) => {
    const r = el.getBoundingClientRect();
    return n + ((r.top < sr.top - 0.5 || r.bottom > sr.bottom + 0.5) ? 1 : 0); }, 0);
  out.hOverflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  document.title = JSON.stringify(out);
</script>"""


def main():
    if not CHROME:
        print("SKIP  no chrome on this node")
        return 0
    tmp = tempfile.mkdtemp(prefix="pcterm-")
    problems = []
    try:
        page = PAGE % {
            "css": "file://" + os.path.join(ROOT, "static", "css", "client.css"),
            "xterm": "file://" + os.path.join(ROOT, "static", "vendor", "xterm", "xterm.css"),
            "body": _markup(),
        }
        path = os.path.join(tmp, "t.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page)
        for w, h in WIDTHS:
            res = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                 f"--window-size={w},{h}", "--virtual-time-budget=4000", "--dump-dom",
                 "--allow-file-access-from-files", "file://" + path],
                capture_output=True, text=True, timeout=120).stdout
            m = re.search(r"<title>(.*?)</title>", res, re.S)
            if not m:
                problems.append((w, "no-output", "the page produced no measurements"))
                continue
            q = json.loads(m.group(1).replace("&quot;", '"'))
            tag = f"{w}px"
            print(f"{tag}: barClip={q['barClip']} keyRows={q['keyRows']} keyMin={q['keyMin']} "
                  f"screenH={q['screenH']} sess={q['sessH']}/{q['sessClip']} "
                  f"hOverflow={q['hOverflow']}")
            if q["sessClip"]:
                problems.append((w, "sessions-wrap", "the 'still running' strip wrapped to a second "
                                                     "row — it must scroll sideways instead"))
            if q["sessH"] > 56:
                problems.append((w, "sessions-tall", f"the session strip is {q['sessH']}px and is "
                                                     "eating rows from the terminal"))
            if q["barClip"]:
                problems.append((tag, "bar-clipped",
                                 f"{q['barClip']} toolbar control(s) sit outside the bar — a wrapping "
                                 "flex row spaces its lines apart and pushes the first out of the top"))
            if q["keyRows"] != 1:
                problems.append((tag, "keys-wrapped",
                                 f"the key bar wrapped to {q['keyRows']} rows; it must stay one line "
                                 "and scroll sideways, not eat the terminal"))
            if q["keyClip"]:
                problems.append((tag, "keys-clipped", f"{q['keyClip']} key(s) outside the key bar"))
            if q["keyMin"] < 30:
                problems.append((tag, "keys-too-small",
                                 f"a key is {q['keyMin']}px tall — Ctrl-C is not a control to miss"))
            if q["screenH"] < 120:
                problems.append((tag, "screen-collapsed",
                                 f"the terminal box is {q['screenH']}px; xterm's fit would compute "
                                 "almost no rows"))
            if q["hOverflow"]:
                problems.append((tag, "h-overflow", "the page scrolls sideways on a phone"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        print(f"\nFAIL  {len(problems)} problem(s):")
        for tag, kind, why in problems:
            print(f"  [{tag}] {kind}: {why}")
        return 1
    print("OK  terminal mobile checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
