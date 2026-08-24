#!/usr/bin/env python3
"""A click on the backdrop must not throw away a post you were writing.

    venv-unified/bin/python scripts/check_composer_dismiss.py

Reported as "clicking outside a reply, quote, new post modal exits the modal, this is getting
annoying and loses stuff". `modal()` dismissed on any backdrop click, which is right for a picker —
nothing to lose, the whole screen is a cancel button — and wrong for the one sheet whose contents are
work: the composer is also the biggest click target in the app, so the miss is easy and the cost is
a half-written post. Autosaving a draft was the previous answer; a draft you have to go and find is
not the post you were writing.

`.modal-sticky` refuses the backdrop. Two halves, and BOTH are the fix:

  1. it must not close — and it must FLINCH, because a click that is silently ignored reads as a
     frozen app, which is a worse bug than the one being removed;
  2. it must offer a visible ✕, because Escape and the Android Back button do not exist on a phone
     browser — a sheet that refuses the backdrop with no other pointer-driven exit is a TRAP, and
     that would be a far worse regression than the dismissal it replaces.

The REAL `modal()`/`closeModal()` are extracted from app.js and run in a real browser against the
real stylesheet, so this cannot pass against a rule that has been edited away. Ordinary sheets are
checked too: dismiss-on-backdrop is the default and must survive.

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")
CHROME = (shutil.which("chromium") or shutil.which("chromium-browser")
          or shutil.which("google-chrome") or shutil.which("google-chrome-stable"))


def _fn(src, head, end):
    """One function, lifted verbatim by matching braces from its signature."""
    i = src.index(head)
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise ValueError(end)


PAGE = r"""<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="%(css)s">
<style>html,body{margin:0;height:100%%;background:#07040f}</style>
<div id="modal-root"></div>
<script>
const $ = (s, r) => (r||document).querySelector(s);
function _trapFocus(){}
function _popKeys(){}
%(modal)s
%(close)s
const out = { cases: [] };
function run(label, cls){
  closeModal();
  modal('<h3 class="cmp-hd">Reply<button class="modal-x" id="x">&#215;</button></h3>'
        + '<textarea id="ta">half a post</textarea>', root => { if(cls) root.classList.add(cls); });
  const bg = document.querySelector('.modal-bg'), box = document.querySelector('.modal');
  const ta = document.getElementById('ta');
  ta.value = 'half a post';
  // A CLICK ON THE BACKDROP ITSELF — the gesture in the report. Aimed at the corner of the layer,
  // which is the part of it a miss actually lands on.
  bg.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  const afterBackdrop = {
    open: !!document.querySelector('.modal'),
    text: (document.getElementById('ta') || {}).value || '',
    nudged: !!(document.querySelector('.modal') || {}).classList
            && document.querySelector('.modal').classList.contains('modal-nudge'),
  };
  // A click INSIDE must never close, sticky or not.
  let insideOpen = null;
  if(afterBackdrop.open){
    document.querySelector('.modal').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    insideOpen = !!document.querySelector('.modal');
  }
  // The visible way out.
  let x = null, closedByX = null;
  if(afterBackdrop.open){
    const btn = document.getElementById('x');
    const r = btn ? btn.getBoundingClientRect() : null;
    x = r ? { w: Math.round(r.width), h: Math.round(r.height), seen: r.width > 0 && r.height > 0 } : null;
    if(btn){ btn.onclick = () => closeModal(); btn.click(); closedByX = !document.querySelector('.modal'); }
  }
  out.cases.push({ label, sticky: cls === 'modal-sticky', afterBackdrop, insideOpen, x, closedByX });
}
try{
  run('an ordinary sheet', '');
  run('the composer', 'modal-sticky');
  out.ok = true;
}catch(e){ out.ok = false; out.err = String((e && e.stack) || e); }
document.title = JSON.stringify(out);
</script>"""


def main():
    if not CHROME:
        print("SKIP  no chrome on this node")
        return 2
    try:
        src = open(APP, encoding="utf-8").read()
        modal_src = _fn(src, "function modal(html, onMount)", "modal")
        close_src = _fn(src, "function closeModal()", "closeModal")
    except ValueError as e:
        print(f"SKIP  could not lift {e} out of app.js — re-point this check")
        return 2

    problems = []

    # THE SOURCE HALF. The browser below proves the RULE; these prove the composer is the sheet it
    # applies to — a rule nothing opts into passes every rendered assertion.
    if "modal-sticky" not in modal_src:
        problems.append(("rule-gone", "modal() no longer honours .modal-sticky — every composer is "
                                      "one stray click from being dismissed again"))
    i = src.index("function compose({")
    body = src[i:src.index("\n  function ", i + 10)]
    if "'modal-sticky'" not in body and '"modal-sticky"' not in body:
        problems.append(("composer-not-sticky",
                         "compose() does not mark its box .modal-sticky, so reply/quote/new post "
                         "still close on a backdrop click"))
    if "cmp-close" not in body:
        problems.append(("composer-has-no-exit",
                         "compose() renders no ✕ — a sheet that refuses the backdrop and has no "
                         "visible close is a trap on a phone browser"))

    page = PAGE % {"css": "file://" + CSS, "modal": modal_src, "close": close_src}
    tmp = tempfile.mkdtemp(prefix="pccmp-")
    try:
        path = os.path.join(tmp, "r.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page)
        try:
            dom = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                 "--window-size=420,860", "--virtual-time-budget=5000", "--dump-dom",
                 "--allow-file-access-from-files", "file://" + path],
                capture_output=True, text=True, timeout=120).stdout
        except subprocess.TimeoutExpired:
            print("FAIL  chrome timed out")
            return 2
        m = re.search(r"<title>(.*?)</title>", dom, re.S)
        if not m:
            print("SKIP  the page produced no measurements")
            return 2
        q = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&")
                       .replace("&lt;", "<").replace("&gt;", ">"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not q.get("ok"):
        print("FAIL  the page threw: " + str(q.get("err")))
        return 1

    for c in q["cases"]:
        ab = c["afterBackdrop"]
        print(f"{c['label']:20s} backdrop-click → {'still open' if ab['open'] else 'closed'}"
              f"{', flinched' if ab['nudged'] else ''}"
              + (f", ✕ {c['x']['w']}x{c['x']['h']}px" if c.get("x") else ""))
        if c["sticky"]:
            if not ab["open"]:
                problems.append(("sticky-dismissed",
                                 "the composer closed on a backdrop click — the text it held is gone"))
                continue
            if ab["text"] != "half a post":
                problems.append(("sticky-lost-text", "the composer survived but its text did not"))
            if not ab["nudged"]:
                problems.append(("no-flinch", "the backdrop click was refused in total silence, "
                                              "which reads as a frozen app"))
            if c["insideOpen"] is False:
                problems.append(("closed-from-inside", "a click INSIDE the composer closed it"))
            if not (c.get("x") or {}).get("seen"):
                problems.append(("no-visible-exit",
                                 "the ✕ has no size on screen — the only pointer-driven way out of "
                                 "a sheet that refuses the backdrop"))
            elif min(c["x"]["w"], c["x"]["h"]) < 28:
                problems.append(("exit-too-small",
                                 f"the ✕ is {c['x']['w']}x{c['x']['h']}px — a target you miss, on "
                                 "the only way out"))
            if c["closedByX"] is False:
                problems.append(("x-does-nothing", "the ✕ did not close the composer"))
        else:
            if ab["open"]:
                problems.append(("default-broken",
                                 "an ordinary sheet no longer closes on a backdrop click — "
                                 "dismiss-on-backdrop is right for a picker and must survive"))

    if problems:
        print()
        for kind, why in problems:
            print(f"FAIL  {kind}: {why}")
        return 1
    print("\nOK  the composer keeps what you typed; every other sheet still dismisses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
