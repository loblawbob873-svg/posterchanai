#!/usr/bin/env python3
"""The add-on's in-page autofill, driven against the REAL login screen.

Run: venv-unified/bin/python scripts/check_extension_autofill.py

Reported as "autofill is buggy and sometimes you click on it, it disappears", and then more
precisely: "when I try to login with nsec, the autofill box disappears". Both are the same shape —
the panel is taken away between the user deciding to click it and the click landing.

There were three causes, and none of them is visible in a unit test:

  * focusout scheduled a hide on a timer, and clicking the badge inside that window opened a panel
    the stale timer then closed. Whether you saw it depended on how fast you clicked.
  * the handlers were on `mousedown`, and the old guard leaned on `:hover` to decide the pointer was
    over the panel — neither of which a touch screen provides, so every tap on Firefox for Android
    raced it.
  * `place()` called hideAll() whenever the remembered field stopped being measurable, and a login
    screen re-renders: the app's own nsec box lives inside a <details>, with no <form> around it.

So this drives the actual client page in a headless browser with the real content script injected
and the extension APIs stubbed: focus the nsec field, click the badge, wait longer than every timer
involved, and require the panel to still be there — then click an entry and require the value to
land in the field.

Exit 0 = clean, 1 = regressions, 2 = could not run (no Chrome).
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
TEMPLATE = os.path.join(ROOT, "templates", "client.html")

STUBS = r"""
window.__errors = [];
window.addEventListener('error', e => window.__errors.push(String(e.message)));
window.browser = {
  runtime: {
    sendMessage: async (m) => {
      if(m.type === 'matches') return { items: [{ id:'a', title:'nostr', username:'me', _match:'exact', hasTotp:true }] };
      if(m.type === 'fill')    return { ok:true, username:'me', password:'nsec1filledbytheextension', totp:'123456' };
      if(m.type === 'known')   return { known:false, rotating:false, id:'' };
      return { ok:true };
    },
    onMessage: { addListener: () => {} },
  },
};
"""

OTP_PAGE = r"""<!doctype html><meta charset="utf-8"><body>
<h1>Two-factor</h1>
<!-- the single-box shape, declared the standard way -->
<form id="one"><input id="code1" type="text" autocomplete="one-time-code" inputmode="numeric" maxlength="6"></form>
<!-- and the six-separate-inputs shape -->
<form id="six"><div id="boxes">
  <input class="d" type="tel" inputmode="numeric" maxlength="1" aria-label="Digit 1">
  <input class="d" type="tel" inputmode="numeric" maxlength="1" aria-label="Digit 2">
  <input class="d" type="tel" inputmode="numeric" maxlength="1" aria-label="Digit 3">
  <input class="d" type="tel" inputmode="numeric" maxlength="1" aria-label="Digit 4">
  <input class="d" type="tel" inputmode="numeric" maxlength="1" aria-label="Digit 5">
  <input class="d" type="tel" inputmode="numeric" maxlength="1" aria-label="Digit 6">
</div></form>
</body>"""

OTP_DRIVE = r"""
(async () => {
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  const out = {};
  const open = async (field) => {
    field.focus();
    field.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
    await wait(200);
    const badge = document.querySelector('.pcpw-badge');
    if(!badge || badge.style.display !== 'block') return null;
    badge.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
    await wait(400);
    return document.querySelector('.pcpw-item');
  };

  // 1. The single box. There is NO password field anywhere on this page — the whole point.
  out.hasPasswordField = !!document.querySelector('input[type="password"]');
  let item = await open(document.querySelector('#code1'));
  out.badgeOnCodeField = !!item;
  out.offersCode = !!(item && /fill the code/i.test(item.textContent));
  if(item){ item.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true })); await wait(350); }
  out.singleFilled = document.querySelector('#code1').value;

  // 2. The six-box shape: one digit per input, not the whole code in the first.
  item = await open(document.querySelector('#six .d'));
  if(item){ item.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true })); await wait(350); }
  out.boxes = [...document.querySelectorAll('#six .d')].map(i => i.value).join('');
  out.errors = window.__errors;
  document.title = 'RESULT' + JSON.stringify(out);
})();
"""

DRIVE = r"""
(async () => {
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  const out = {};
  // The gate ships hidden and the app reveals it on load; there is no app JS here, so do it.
  const gate = document.querySelector('#auth-gate');
  if(gate) gate.classList.remove('hidden');
  const field = document.querySelector('#nsec-input');
  out.foundField = !!field;
  if(!field){ document.title = 'RESULT' + JSON.stringify(out); return; }

  // The field lives inside a <details>; open it the way a user would.
  const det = document.querySelector('#auth-key');
  if(det) det.open = true;
  field.focus();
  field.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
  await wait(250);

  const badge = document.querySelector('.pcpw-badge');
  out.badgeShown = !!(badge && badge.style.display === 'block');
  if(!badge){ document.title = 'RESULT' + JSON.stringify(out); return; }

  // THE RACE: a blur lands (the page moves focus, the details re-lays out) and the user clicks the
  // badge immediately after. The old build scheduled a hide here and honoured it.
  field.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
  badge.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
  await wait(120);
  const panel = document.querySelector('.pcpw-panel');
  out.panelOpenedAt120 = !!(panel && panel.style.display === 'block');

  // Longer than every timer in play (the hide was 180-250ms).
  await wait(600);
  out.panelStillOpen = !!(panel && panel.style.display === 'block');
  out.entries = document.querySelectorAll('.pcpw-item').length;

  // And a re-render of the field must not take it away either.
  const clone = field.cloneNode(true);
  field.replaceWith(clone);
  window.dispatchEvent(new Event('scroll'));
  await wait(250);
  out.survivesRerender = !!(panel && panel.style.display === 'block');

  const item = document.querySelector('.pcpw-item');
  if(item){
    item.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
    await wait(400);
  }
  const now = document.querySelector('#nsec-input');
  out.filled = !!(now && now.value === 'nsec1filledbytheextension');
  out.closedAfterFill = !!(panel && panel.style.display !== 'block');
  out.errors = window.__errors;
  document.title = 'RESULT' + JSON.stringify(out);
})();
"""


def _run_page(chrome, html, drive, content_js, content_css):
    """Render one page with the real content script injected; return what it reported."""
    html = html.replace("</body>",
                        f"<style>{content_css}</style>\n"
                        f"<script>{STUBS}</script>\n"
                        f"<script>{content_js}</script>\n"
                        f"<script>{drive}</script>\n</body>")
    d = tempfile.mkdtemp()
    page = os.path.join(d, "page.html")
    try:
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(html)
        p = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=6000",
             f"--user-data-dir={d}/prof", "--dump-dom", "file://" + page],
            capture_output=True, text=True, timeout=120)
        m = re.search(r"RESULT(\{.*?\})</title>", p.stdout, re.S)
        return json.loads(m.group(1)) if m else None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2

    # The real login markup, with the Jinja bits stripped — this is about the DOM shape the add-on
    # meets (a password input inside a <details>, no <form>), not about the server.
    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    html = re.sub(r"\{%.*?%\}", "", html, flags=re.S)
    html = re.sub(r"\{\{.*?\}\}", "", html, flags=re.S)
    html = re.sub(r"<script[^>]*src=[^>]*></script>", "", html)      # no app JS: this tests the add-on
    html = html.replace("<body>", "<body><style>.hidden{display:none}.auth-gate{position:static}</style>")

    with open(os.path.join(EXT, "content.js"), encoding="utf-8") as fh:
        content_js = fh.read()
    with open(os.path.join(EXT, "content.css"), encoding="utf-8") as fh:
        content_css = fh.read()

    html = html.replace("</body>",
                        f"<style>{content_css}</style>\n"
                        f"<script>{STUBS}</script>\n"
                        f"<script>{content_js}</script>\n"
                        f"<script>{DRIVE}</script>\n</body>")

    d = tempfile.mkdtemp()
    page = os.path.join(d, "login.html")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(html)
    try:
        p = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=6000",
             f"--user-data-dir={d}/prof", "--dump-dom", "file://" + page],
            capture_output=True, text=True, timeout=120)
        m = re.search(r"RESULT(\{.*?\})</title>", p.stdout, re.S)
        if not m:
            print("SKIP  the page never reported")
            return 2
        got = json.loads(m.group(1))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    otp = _run_page(chrome, OTP_PAGE, OTP_DRIVE, content_js, content_css)
    print("autofill:", json.dumps(got))
    print("2fa page:", json.dumps(otp))
    problems = []
    if not got.get("foundField"):
        print("SKIP  the login template has no #nsec-input any more")
        return 2
    if not got.get("badgeShown"):
        problems.append(("no-badge", "focusing the nsec field showed no PosterChan badge"))
    if not got.get("panelOpenedAt120"):
        problems.append(("panel-vanishes", "clicking the badge did not open the panel"))
    if not got.get("panelStillOpen"):
        problems.append(("panel-vanishes",
                         "the panel was open and then disappeared on its own — a blur-scheduled hide "
                         "fired after the click that opened it"))
    if not got.get("entries"):
        problems.append(("panel-empty", "the matching login did not render in the panel"))
    if not got.get("survivesRerender"):
        problems.append(("panel-vanishes",
                         "re-rendering the field closed the open panel — login screens do that "
                         "constantly, and the nsec box lives in a <details>"))
    if not got.get("filled"):
        problems.append(("fill-failed", "clicking the entry did not put the password in the field"))
    if not got.get("closedAfterFill"):
        problems.append(("panel-stuck", "the panel stayed open after filling"))
    for e in (got.get("errors") or []):
        problems.append(("script-error", e))

    # The 2FA step: a page with no password field at all, which is where a code is actually needed
    # and where the add-on used to be completely silent.
    if otp is None:
        problems.append(("totp-not-filled", "the 2FA page never reported"))
    else:
        if otp.get("hasPasswordField"):
            problems.append(("totp-not-filled", "the 2FA fixture is wrong — it has a password field"))
        if not otp.get("badgeOnCodeField"):
            problems.append(("totp-not-filled",
                             "no badge on a one-time-code field: the second step of every 2FA login "
                             "has no password field, so nothing offered the code"))
        if not otp.get("offersCode"):
            problems.append(("totp-not-filled", "the panel on a code field did not offer the code"))
        if otp.get("singleFilled") != "123456":
            problems.append(("totp-not-filled",
                             f"the code box holds {otp.get('singleFilled')!r}, expected '123456'"))
        if otp.get("boxes") != "123456":
            problems.append(("totp-not-filled",
                             f"the six-box form holds {otp.get('boxes')!r} — a split code entry must "
                             "get one digit per box, not the whole code in the first"))
        for e in (otp.get("errors") or []):
            problems.append(("script-error", "2fa page: " + e))

    if problems:
        print("\nREGRESSIONS")
        for kind, msg in problems:
            print(f"  {kind}: {msg}")
        return 1
    print("OK  extension autofill checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
