#!/usr/bin/env python3
"""A half-written post must survive a click on the desktop.

    venv-unified/bin/python scripts/check_composer_survives_a_desktop_click.py [BASE]

Reported, after the backdrop fix had already shipped: "new post in social exited when I click on
desktop so that is broke still".

WHY THE EXISTING COVERAGE DID NOT SEE IT. There are two composer tests and neither opens the
composer:

  * `tests/client/test_every_composer_is_sticky.py` asserts the CLASS is applied and the close
    button is wired. It never clicks anything.
  * `scripts/check_composer_dismiss.py` EXTRACTS `modal()` and drives it in a browser — so it proves
    `modal()` is right, which it is. It never runs the app's own `compose()`, and never inside the
    windowed desktop, where the click lands on `#os-desk` rather than on the modal backdrop.

So this one uses the REAL client, signs in with a throwaway key, opens the REAL composer through
`__PC.compose()`, types into it, and then clicks the things a person clicks: the desktop background,
and the backdrop. It asserts the sheet is still there AND that the text is still in it — a composer
that survives while being rebuilt empty is the same lost post.

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

PORT = int(os.environ.get("PC_CHECK_PORT") or 9491)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-composer-desk-check"
BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PC_ORIGIN") or "http://127.0.0.1:3051"
SK = os.urandom(32).hex()
TYPED = "half written post that must not be lost"

OPEN = """(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  if (!window.__PC || !__PC.compose) return {ok:false, why:'the client never exposed compose()'};
  /* WHO CLOSES IT. closeModal() clears #modal-root.innerHTML, so patching that one element's setter
     catches the caller SYNCHRONOUSLY — a MutationObserver runs as a microtask and by then the stack
     that did it is gone. */
  if (!window.__cmpPatched) {
    window.__cmpPatched = true;
    const root = document.querySelector('#modal-root');
    const d = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
    Object.defineProperty(root, 'innerHTML', {
      configurable: true,
      get() { return d.get.call(this); },
      set(v) { if (v === '') { try { window.__who = new Error('cleared').stack; } catch (e) {} }
               d.set.call(this, v); }
    });
  }
  window.__who = null;
  __PC.compose({});
  for (let i = 0; i < 40; i++) {
    await wait(100);
    if (document.querySelector('#modal-root .modal')) break;
  }
  const box = document.querySelector('#modal-root .modal');
  if (!box) return {ok:false, why:'compose() opened no sheet'};
  const ta = box.querySelector('textarea');
  if (!ta) return {ok:false, why:'the composer has no textarea'};
  ta.value = %s;
  ta.dispatchEvent(new Event('input', {bubbles:true}));
  const bg = document.querySelector('#modal-root .modal-bg');
  const first = bg && bg.querySelector('.modal');
  window.__cmpDiag = {boxCls: box.className, bgCls: bg ? bg.className : null,
    firstIsBox: first === box, firstCls: first ? first.className : null,
    nModals: document.querySelectorAll('#modal-root .modal').length,
    nBgs: document.querySelectorAll('#modal-root .modal-bg').length,
    hasOnclick: !!(bg && bg.onclick)};
  return {ok:true, sticky: box.classList.contains('modal-sticky'),
          diag: window.__cmpDiag,
          desktop: !!(window.PCOS && PCOS.isOn && PCOS.isOn())};
})()""" % json.dumps(TYPED)

# A click where a person clicks it: real pointer events, on the element that is actually under the
# cursor at that point — not a synthetic call to a handler we already believe in.
CLICK = """(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const sel = %s;
  const target = document.querySelector(sel);
  if (!target) return {ok:false, why:'no ' + sel + ' on this page'};
  const r = target.getBoundingClientRect();
  const x = Math.round(r.left + Math.min(30, r.width / 2)), y = Math.round(r.top + Math.min(30, r.height / 2));
  const at = document.elementFromPoint(x, y);
  for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) {
    (at || target).dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y}));
  }
  await wait(400);
  const box = document.querySelector('#modal-root .modal');
  const ta = box && box.querySelector('textarea');
  return {ok:true, open: !!box, text: ta ? ta.value : null,
          hit: at ? (at.id || at.className || at.tagName) : null,
          diag: window.__cmpDiag || null, who: window.__who || null};
})()"""


async def main():
    chrome = (shutil.which("google-chrome") or shutil.which("chromium")
              or shutil.which("chromium-browser") or "/opt/google/chrome/chrome")
    if not os.path.exists(chrome) and not shutil.which(chrome):
        print("SKIP  no chrome on this node"); return 2
    try:
        urllib.request.urlopen(BASE + "/client", timeout=6)
    except Exception as e:
        print("SKIP  no instance at %s (%s)" % (BASE, e)); return 2

    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                             "--window-size=1600,1000",
                             f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                             "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import websockets
    except Exception:
        proc.terminate(); print("SKIP  websockets module missing"); return 2
    try:
        ws_url = None
        for _ in range(60):
            await asyncio.sleep(0.4)
            try:
                d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2))
                ws_url = d["webSocketDebuggerUrl"]; break
            except Exception:
                continue
        if not ws_url:
            print("SKIP  chrome never opened its debugging port"); return 2

        async with websockets.connect(ws_url, max_size=80 * 1024 * 1024) as ws:
            seq = [0]

            async def call(method, params=None):
                seq[0] += 1
                await ws.send(json.dumps({"id": seq[0], "method": method, "params": params or {}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == seq[0]:
                        return m.get("result", {})

            tgt = await call("Target.createTarget", {"url": "about:blank"})
            sess = (await call("Target.attachToTarget",
                               {"targetId": tgt["targetId"], "flatten": True}))["sessionId"]

            async def send(method, params=None):
                seq[0] += 1
                await ws.send(json.dumps({"id": seq[0], "method": method,
                                          "params": params or {}, "sessionId": sess}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == seq[0]:
                        return m.get("result", {})

            async def js(expr):
                r = await send("Runtime.evaluate",
                               {"expression": expr, "awaitPromise": True, "returnByValue": True})
                return (r.get("result") or {}).get("value")

            await send("Page.enable"); await send("Runtime.enable")
            await send("Page.addScriptToEvaluateOnNewDocument",
                       {"source": "try{localStorage.setItem('pc_nostr_session',JSON.stringify({sk:%s}));"
                                  "localStorage.setItem('pc.os.on','1');}catch(e){}" % json.dumps(SK)})
            await send("Page.navigate", {"url": BASE + "/client"})
            for _ in range(80):
                await asyncio.sleep(0.4)
                if await js("!!(window.__PC && window.__PC.compose)"):
                    break

            # ENTER THE WINDOWED DESKTOP, which is where it was reported. Without this the
            # `#os-desk` half silently does not run — and a half that does not run is the reason
            # this bug reached a user twice.
            entered = await js("(async () => {"
                               "  const wait = ms => new Promise(r => setTimeout(r, ms));"
                               "  if (!(window.PCOS && PCOS.enter)) return 'no PCOS';"
                               "  if (PCOS.isOn && PCOS.isOn()) return 'already on';"
                               "  try { PCOS.enter(); } catch (e) { return 'threw: ' + e; }"
                               "  for (let i = 0; i < 40; i++) { await wait(100);"
                               "    if (document.querySelector('#os-desk')) return 'on'; }"
                               "  return 'no #os-desk after enter';"
                               "})()")
            if entered not in ("on", "already on"):
                print("SKIP  could not enter the windowed desktop (%s) — the half this check "
                      "exists for would not run" % entered)
                return 2

            problems, unchecked = [], []
            for where, selector in (("the desktop background", "'#os-desk'"),
                                    ("the modal backdrop", "'#modal-root .modal-bg'")):
                await js("document.querySelector('#modal-root').innerHTML='';1")
                opened = await js(OPEN)
                if not isinstance(opened, dict) or not opened.get("ok"):
                    print("SKIP  could not open the composer: %s" % (opened,)); return 2
                if not opened.get("sticky"):
                    problems.append("the composer is not marked sticky at all")
                got = await js(CLICK % selector)
                if got is None:
                    print("SKIP  the click probe did not evaluate for %s — this check proves "
                          "nothing until that is fixed" % where)
                    return 2
                if not isinstance(got, dict) or not got.get("ok"):
                    why = (got or {}).get("why") or got
                    # A missing #os-desk is a real absence (no windowed desktop at this viewport),
                    # not a pass: say which half went unchecked.
                    print("note: %s was not exercised — %s" % (where, why))
                    unchecked.append(where)
                    continue
                print("   diag: %s" % json.dumps(got.get("diag")))
                if got.get("who"):
                    print("   closed by:\n      " + "\n      ".join(
                        str(got["who"]).splitlines()[:6]))
                if not got.get("open"):
                    problems.append("clicking %s CLOSED the composer (hit %s)"
                                    % (where, got.get("hit")))
                elif got.get("text") != TYPED:
                    problems.append("clicking %s kept the sheet but lost the text (%r) — a composer "
                                    "rebuilt empty is the same lost post"
                                    % (where, got.get("text")))
                # Unconditionally. `__PC.closeModal && __PC.closeModal()` short-circuits when the
                # client does not expose it, the catch never runs, and the next iteration opens a
                # SECOND sheet on top of the first — which is a different situation from the one
                # being measured, and it read as a failure of the product.
                await js("document.querySelector('#modal-root').innerHTML='';"
                         "document.body.classList.remove('modal-open');1")

            print(("FAIL  " if problems else "OK    ")
                  + "a half-written post survives a click on the desktop and on the backdrop")
            for p in problems:
                print("  - %s" % p)
            for u in unchecked:
                print("  ~ not exercised: %s" % u)
            if problems:
                return 1
            return 2 if unchecked else 0
    finally:
        proc.terminate()
        shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
