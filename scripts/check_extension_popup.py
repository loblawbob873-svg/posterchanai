#!/usr/bin/env python3
"""The Firefox add-on's popup actually renders.

Run: venv-unified/bin/python scripts/check_extension_popup.py

A browser-action popup has exactly two ways to come out as a thin vertical line, and this screen hit
one of them in the wild:

  1. SIZED IN VIEWPORT UNITS. A popup has no viewport of its own to measure — Firefox lays the
     document out in order to discover how big the popup should be — so `100vw`/`100vh` resolve
     against a width and height that do not exist yet, and `width: min(380px, 100vw)` picks 0.
     Chrome resolves those units against the screen instead, so the bug is invisible there, which is
     why this half is a STATIC check of the stylesheet and not a render.

  2. THE SCRIPT THREW. Every pane in popup.html starts `hidden` and popup.js reveals one; if it dies
     first — a missing vaultcore.js, a renamed element, a bad API — the body is empty and the popup
     collapses to nothing. That half IS a render, in headless Chrome, with the extension APIs
     stubbed: it catches a popup that loads no core, wires no handlers, or shows no pane.

Neither half subsumes the other. Exit 0 = clean, 1 = regressions, 2 = could not run.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")


# ---------------------------------------------------------------- 1. the stylesheet

def css_problems():
    """The popup's own box may not be measured in viewport units."""
    out = []
    with open(os.path.join(EXT, "popup.css"), encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)      # comments may discuss vw/vh freely

    for sel in ("html", "body"):
        m = re.search(r"(?:^|\})\s*" + sel + r"\s*\{([^}]*)\}", css, re.S)
        if not m:
            out.append(("popup-collapses", f"popup.css has no `{sel}` rule — the popup has no size of its own"))
            continue
        block = m.group(1)
        if re.search(r"\d\s*v[wh]\b", block):
            out.append(("popup-collapses",
                        f"`{sel}` is sized in viewport units: {block.strip()[:70]!r}. A popup has no "
                        "viewport yet, so Firefox resolves that to 0 and renders a thin line."))
        if sel == "body" and not re.search(r"width\s*:\s*\d+px", block):
            out.append(("popup-collapses", "`body` has no absolute width — Firefox needs one to size the popup"))

    # A scroll cap in vh is the same trap one level down: the list disappears instead of the popup.
    for m in re.finditer(r"([#.][\w-]+)\s*\{([^}]*max-height\s*:\s*[^;]*v[wh][^;]*)", css, re.S):
        out.append(("popup-collapses",
                    f"{m.group(1)} caps its height in viewport units — 0 inside a popup"))
    return out


# ---------------------------------------------------------------- 2. it renders

PAGE_STUBS = r"""
// Enough of the WebExtension API for the popup to boot. It talks to a background that answers as if
// a vault were paired, so the list path renders rather than only the pairing screen.
window.__errors = [];
window.addEventListener('error', e => window.__errors.push(String(e.message)));
window.addEventListener('unhandledrejection', e => window.__errors.push('rejection: ' + e.reason));
const ITEMS = [{ id:'a', title:'GitHub', username:'me@example.com', _match:'exact', hasTotp:true },
               { id:'b', title:'Gist', username:'me', _match:'domain', hasTotp:false }];
window.browser = {
  runtime: { sendMessage: async (m) => {
    if(m.type === 'state')   return { paired:true, mode:'ro', count:2, status:'ready', lastSync:0 };
    if(m.type === 'matches') return { items: ITEMS };
    if(m.type === 'reveal')  return { ok:true, username:'me', password:'pw', totp:'123456', left:22 };
    return { ok:true };
  }},
  tabs: { query: async () => [{ id:1, url:'https://github.com/login' }], sendMessage: async () => {} },
};
"""


def render_problems():
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        return None, "no Chrome"

    with open(os.path.join(EXT, "popup.html"), encoding="utf-8") as fh:
        html = fh.read()
    # vaultcore.js is a real file in the extension dir; the stub goes before popup.js so `browser`
    # exists when it runs.
    html = html.replace('<script src="popup.js"></script>',
                        "<script>" + PAGE_STUBS + "</script>\n"
                        '<script src="popup.js"></script>\n'
                        "<script>setTimeout(() => {\n"
                        "  const vis = el => !!(el && el.getClientRects().length);\n"
                        "  const panes = ['pane-pair','pane-list','pane-gen'].filter(p => vis(document.getElementById(p)));\n"
                        "  const b = document.body.getBoundingClientRect();\n"
                        "  const items = document.querySelectorAll('.item').length;\n"
                        "  const btns = [...document.querySelectorAll('.it-a button')]\n"
                        "                  .map(x => Math.round(x.getBoundingClientRect().height));\n"
                        "  const out = { w: Math.round(b.width), h: Math.round(b.height), panes, items,\n"
                        "                btns, core: typeof PCVaultCore, errors: window.__errors };\n"
                        "  document.title = 'RESULT' + JSON.stringify(out);\n"
                        "}, 700);</script>")

    d = tempfile.mkdtemp()
    tmp = os.path.join(EXT, ".popup-check.html")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(html)
        p = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=3000",
             f"--user-data-dir={d}", "--dump-dom", "file://" + tmp],
            capture_output=True, text=True, timeout=120)
        m = re.search(r"RESULT(\{.*?\})</title>", p.stdout, re.S)
        if not m:
            return None, "the popup never reported (it may not have run at all)"
        return json.loads(m.group(1)), None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
        shutil.rmtree(d, ignore_errors=True)


def main():
    problems = list(css_problems())

    got, why = render_problems()
    if got is None:
        print(f"SKIP  render half: {why}")
    else:
        print(f"popup: {got['w']}x{got['h']} panes={got['panes']} items={got['items']} "
              f"core={got['core']} errors={len(got['errors'])}")
        if got["errors"]:
            problems.append(("popup-script-died", f"the popup threw: {got['errors'][:2]}"))
        if got["core"] != "object":
            problems.append(("popup-script-died",
                             "PCVaultCore is not loaded — the generator and the TOTP codes are dead"))
        if not got["panes"]:
            problems.append(("popup-collapses",
                             "no pane is visible: every one starts hidden and the script reveals one, "
                             "so an empty popup means the script never got that far"))
        if got["w"] < 300:
            problems.append(("popup-collapses", f"the popup is {got['w']}px wide"))
        if got["h"] < 120:
            problems.append(("popup-collapses", f"the popup is {got['h']}px tall"))
        if not got["items"]:
            problems.append(("popup-empty", "the matching logins did not render"))
        if got["btns"] and min(got["btns"]) < 32:
            problems.append(("tiny-tap-target",
                             f"a row button is {min(got['btns'])}px tall — this is a phone popup too"))

    if problems:
        print("\nREGRESSIONS")
        for kind, msg in problems:
            print(f"  {kind}: {msg}")
        return 1
    print("OK  extension popup checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
