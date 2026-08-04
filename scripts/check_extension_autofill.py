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

Driven over CDP with REAL mouse input, not synthetic events: the content script refuses anything
whose `isTrusted` is false, because a page can otherwise dispatch its own PointerEvent at the badge
and drive the entire fill flow with no human involved. A harness that could exercise it with
synthetic events would be testing something other than what ships.

So this drives the actual client page in a headless browser with the real content script injected
and the extension APIs stubbed: focus the nsec field, click the badge, wait longer than every timer
involved, and require the panel to still be there — then click an entry and require the value to
land in the field.

Exit 0 = clean, 1 = regressions, 2 = could not run (no Chrome).
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

PORT = 9483

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
const wantClick = async (sel) => {
  window.__clickWanted = sel;
  for(let i=0;i<60 && window.__clickWanted;i++) await new Promise(r=>setTimeout(r,80));
  await new Promise(r=>setTimeout(r,250));
};
const finish = (o) => { window.__result = o; window.__clickWanted = '__done'; };

  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  const out = {};
  const open = async (field) => {
    field.focus();
    field.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
    await wait(200);
    const badge = document.querySelector('.pcpw-badge');
    if(!badge || badge.style.display !== 'block') return null;
    await wantClick('.pcpw-badge');
    return document.querySelector('.pcpw-item');
  };

  // 1. The single box. There is NO password field anywhere on this page — the whole point.
  out.hasPasswordField = !!document.querySelector('input[type="password"]');
  let item = await open(document.querySelector('#code1'));
  out.badgeOnCodeField = !!item;
  out.offersCode = !!(item && /fill the code/i.test(item.textContent));
  if(item) await wantClick('.pcpw-item');
  out.singleFilled = document.querySelector('#code1').value;

  // 2. The six-box shape: one digit per input, not the whole code in the first.
  item = await open(document.querySelector('#six .d'));
  if(item) await wantClick('.pcpw-item');
  out.boxes = [...document.querySelectorAll('#six .d')].map(i => i.value).join('');
  out.errors = window.__errors;
  finish(out);
})();
"""

DRIVE = r"""
(async () => {
const wantClick = async (sel) => {
  window.__clickWanted = sel;
  for(let i=0;i<60 && window.__clickWanted;i++) await new Promise(r=>setTimeout(r,80));
  await new Promise(r=>setTimeout(r,250));
};
const finish = (o) => { window.__result = o; window.__clickWanted = '__done'; };

  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  const out = {};
  // The gate ships hidden and the app reveals it on load; there is no app JS here, so do it.
  const gate = document.querySelector('#auth-gate');
  if(gate) gate.classList.remove('hidden');
  const field = document.querySelector('#nsec-input');
  out.foundField = !!field;
  if(!field){ finish(out); return; }

  // The field lives inside a <details>; open it the way a user would.
  const det = document.querySelector('#auth-key');
  if(det) det.open = true;
  field.focus();
  field.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
  await wait(250);

  const badge = document.querySelector('.pcpw-badge');
  out.badgeShown = !!(badge && badge.style.display === 'block');
  if(!badge){ finish(out); return; }

  // THE RACE: a blur lands (the page moves focus, the details re-lays out) and the user clicks the
  // badge immediately after. The old build scheduled a hide here and honoured it.
  // A real mouse press on the badge already produces the blur this race needs; dispatching a
  // synthetic focusout as well fired the hide path twice and is not what a user does.
  await wantClick('.pcpw-badge');
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

  if(document.querySelector('.pcpw-item')) await wantClick('.pcpw-item');
  const now = document.querySelector('#nsec-input');
  out.filled = !!(now && now.value === 'nsec1filledbytheextension');
  out.closedAfterFill = !!(panel && panel.style.display !== 'block');
  out.errors = window.__errors;
  finish(out);
})();
"""


async def _run_page_cdp(chrome, html, drive, content_js, content_css):
    """Render one page with the real content script injected, driving it with REAL input.

    CDP rather than --dump-dom, because the content script refuses events whose `isTrusted` is
    false — a page can otherwise dispatch its own PointerEvent at the badge and drive the whole
    fill flow with no human involved. Synthetic events cannot exercise it, and weakening the guard
    to suit the harness would be testing something other than what ships. Input.dispatchMouseEvent
    produces the real thing.
    """
    import websockets
    html = html.replace("</body>",
                        f"<style>{content_css}</style>\n"
                        f"<script>{STUBS}</script>\n"
                        f"<script>{content_js}</script>\n"
                        f"<script>{drive}</script>\n</body>")
    d = tempfile.mkdtemp()
    page_file = os.path.join(d, "page.html")
    with open(page_file, "w", encoding="utf-8") as fh:
        fh.write(html)
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={d}/prof",
         "--window-size=1100,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                target = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not target:
            return None
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Page.navigate", {"url": "file://" + page_file})
            await asyncio.sleep(1.0)

            # A real click, at real coordinates, on whatever selector the page asks for next.
            async def pump():
                for _ in range(80):
                    r = await call("Runtime.evaluate",
                                   {"expression": "window.__clickWanted || ''", "returnByValue": True})
                    sel = (r.get("result") or {}).get("value") or ""
                    if sel == "__done":
                        break
                    if sel:
                        box = await call("Runtime.evaluate", {"returnByValue": True, "expression": f"""
                            (() => {{ const el = document.querySelector({json.dumps(sel)});
                                      if(!el) return null; const r = el.getBoundingClientRect();
                                      return {{x: r.x + r.width/2, y: r.y + r.height/2}}; }})()"""})
                        pt = (box.get("result") or {}).get("value")
                        if pt:
                            # A move first, then press with `buttons` set: Chrome will not synthesise
                            # a pointerdown from a press that never had the pointer over the target.
                            await call("Input.dispatchMouseEvent",
                                       {"type": "mouseMoved", "x": pt["x"], "y": pt["y"],
                                        "buttons": 0, "pointerType": "mouse"})
                            await call("Input.dispatchMouseEvent",
                                       {"type": "mousePressed", "x": pt["x"], "y": pt["y"],
                                        "button": "left", "buttons": 1, "clickCount": 1,
                                        "pointerType": "mouse"})
                            await call("Input.dispatchMouseEvent",
                                       {"type": "mouseReleased", "x": pt["x"], "y": pt["y"],
                                        "button": "left", "buttons": 0, "clickCount": 1,
                                        "pointerType": "mouse"})
                        await call("Runtime.evaluate", {"expression": "window.__clickWanted = ''"})
                    await asyncio.sleep(0.15)

            await pump()
            r = await call("Runtime.evaluate",
                           {"expression": "window.__result ? JSON.stringify(window.__result) : ''",
                            "returnByValue": True})
            val = (r.get("result") or {}).get("value") or ""
            return json.loads(val) if val else None
    finally:
        proc.terminate()
        shutil.rmtree(d, ignore_errors=True)


def _run_page(chrome, html, drive, content_js, content_css):
    return asyncio.run(_run_page_cdp(chrome, html, drive, content_js, content_css))


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

    got = _run_page(chrome, html, DRIVE, content_js, content_css)
    if got is None:
        print("SKIP  the login page never reported")
        return 2

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
