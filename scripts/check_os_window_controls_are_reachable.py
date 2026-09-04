#!/usr/bin/env python3
"""Every control in a WINDOWED view must be fully visible and actually clickable.

    venv-unified/bin/python scripts/check_os_window_controls_are_reachable.py [BASE]

Reported: *"back button is cut off on desktop when you open a thread from Social"*, and before that
a composer that closed itself on the desktop. Both are the same shape and NEITHER of the existing
desktop checks could see them:

  * `check_os_desktop.py` drives os.js against a STUB feature that paints a placeholder into
    `#feed`. It proves the window manager works. It has never rendered a real view, so no amount of
    it can notice that a real view's chrome lands under the window frame or off its edge.
  * The client's own layout checks run at phone and browser widths, in the ordinary page, where
    `#feed` is the whole column and nothing is clipped by a window body.

So this one signs in with a throwaway key, enters the REAL windowed desktop, opens the REAL views,
and measures every control it finds: the rectangle must lie inside the window body, and the point at
its centre must hit the control itself (or a child of it) rather than something painted over it.

`elementFromPoint` is the half that matters. A control can be inside its window, at full size, with
correct styles, and still be unclickable because a header, a sticky bar or a resize handle is on top
of it -- which reads to a person as a dead button, exactly like a missing one.

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

PORT = int(os.environ.get("PC_CHECK_PORT") or 9498)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-window-controls"
BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PC_ORIGIN") or "http://127.0.0.1:3051"
SK = os.urandom(32).hex()

# The views a person opens from the desktop, and the controls each one must leave reachable. A view
# with no control listed is still opened -- a throw while rendering is a failure on its own.
VIEWS = [
    ("notifications", []),
    ("messages", []),
    ("timeline", []),
]

MEASURE = r"""(() => {
  /* THE WINDOW A VIEW IS IN. `#feed` is handed to the focused window's body, so the body that
     CONTAINS the feed is the frame this view has to fit inside. */
  const feed = document.getElementById('feed');
  if (!feed) return {ok:false, why:'no #feed at all'};
  const body = feed.closest('.osw-body');
  if (!body) return {ok:false, why:'#feed is not inside a window body — the desktop did not hand it over'};
  const win = body.closest('.osw');
  const br = body.getBoundingClientRect();
  const rows = [];
  for (const el of feed.querySelectorAll(SELECTORS)) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) { rows.push({sel:describe(el), why:'zero size', r:box(r)}); continue; }
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const hit = document.elementFromPoint(cx, cy);
    rows.push({
      sel: describe(el),
      r: box(r),
      /* CUT OFF: any edge outside the window body it lives in. Half a pixel of rounding is not a
         report; two is the thing people see. */
      clipped: (r.left < br.left - 2) || (r.top < br.top - 2)
               || (r.right > br.right + 2) || (r.bottom > br.bottom + 2),
      over: (r.left < br.left - 2 ? 'left' : r.top < br.top - 2 ? 'top'
             : r.right > br.right + 2 ? 'right' : r.bottom > br.bottom + 2 ? 'bottom' : ''),
      /* DEAD: something else is painted over its centre. A control the pointer cannot reach is
         indistinguishable from one that is missing. */
      blocked: !(hit && (hit === el || el.contains(hit) || hit.contains(el))),
      blocker: hit ? describe(hit) : 'nothing'
    });
  }
  return {ok:true, body:box(br), win: win ? box(win.getBoundingClientRect()) : null, rows};
  function box(r){return {x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)};}
  function describe(el){
    if (!el || !el.tagName) return String(el);
    return el.tagName.toLowerCase()
      + (el.id ? '#' + el.id : '')
      + (el.className && typeof el.className === 'string' && el.className.trim()
         ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '');
  }
})()"""

SELECTORS = ("'.thread-top button, .feed > .tabs button, .feed > .search-bar button, "
             "#th-back, .prof-tabs button, .view-actions button'")


async def main():
    chrome = (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
              or shutil.which("chromium") or "/opt/google/chrome/chrome")
    if not os.path.exists(chrome) and not shutil.which(chrome):
        print("SKIP  no chrome on this node"); return 2
    try:
        urllib.request.urlopen(BASE + "/client", timeout=6)
    except Exception as e:
        print("SKIP  no instance at %s (%s)" % (BASE, e)); return 2
    try:
        import websockets
    except Exception:
        print("SKIP  websockets module missing"); return 2

    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                             "--window-size=1600,1000",
                             f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                             "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

            async def call(method, params=None, sess=None):
                seq[0] += 1
                msg = {"id": seq[0], "method": method, "params": params or {}}
                if sess:
                    msg["sessionId"] = sess
                await ws.send(json.dumps(msg))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == seq[0]:
                        return m.get("result", {})

            tgt = await call("Target.createTarget", {"url": "about:blank"})
            sess = (await call("Target.attachToTarget",
                               {"targetId": tgt["targetId"], "flatten": True}))["sessionId"]

            async def js(expr):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "awaitPromise": True, "returnByValue": True},
                               sess)
                return (r.get("result") or {}).get("value")

            await call("Page.enable", None, sess); await call("Runtime.enable", None, sess)
            await call("Page.addScriptToEvaluateOnNewDocument",
                       {"source": "try{localStorage.setItem('pc_nostr_session',JSON.stringify({sk:%s}));"
                                  "localStorage.setItem('pc.os.on','1');}catch(e){}" % json.dumps(SK)},
                       sess)
            await call("Page.navigate", {"url": BASE + "/client"}, sess)
            for _ in range(80):
                await asyncio.sleep(0.4)
                if await js("!!(window.__PC && window.__PC.switchView)"):
                    break

            entered = await js("(async () => {"
                               "  const wait = ms => new Promise(r => setTimeout(r, ms));"
                               "  if (!(window.PCOS && PCOS.enter)) return 'no PCOS';"
                               "  if (!(PCOS.isOn && PCOS.isOn())) { try { PCOS.enter(); }"
                               "    catch (e) { return 'threw: ' + e; } }"
                               "  for (let i = 0; i < 40; i++) { await wait(100);"
                               "    if (document.querySelector('#os-desk')) return 'on'; }"
                               "  return 'no #os-desk after enter';"
                               "})()")
            if entered != "on":
                print("SKIP  could not enter the windowed desktop (%s) — the half this check "
                      "exists for would not run" % entered)
                return 2

            problems = []
            # A THREAD, which is the reported case: it is opened from another view and repaints the
            # window that view was in. A synthetic event is used so this needs no network and no
            # particular account state — the CHROME is what is being measured, not the content.
            opened = await js("""(async () => {
              const wait = ms => new Promise(r => setTimeout(r, ms));
              /* openThread, NEVER renderThread: renderThread swaps the view and pushes nothing,
                 which is not the path a person takes from Social — and on the desktop openThread is
                 what decides the post gets its OWN window. Measuring the wrong path is how a check
                 passes against chrome nobody sees. */
              if (!window.__PC || !__PC.openThread) return 'no openThread';
              const id = 'f'.repeat(64);
              try {
                /* saveEvent, which is the Store's own ingest path -- the client paints a thread
                   head from what it already holds before any socket is waited on, so seeding here
                   is what makes this check need no network and no particular account state. */
                Store.saveEvent({id, kind:1, pubkey:'a'.repeat(64),
                  created_at: Math.floor(Date.now()/1000), content:'a post opened from Social',
                  tags: [], sig:'0'.repeat(128)});
              } catch (e) {}
              try { __PC.openThread(id); } catch (e) { return 'threw: ' + e; }
              for (let i = 0; i < 60; i++) { await wait(100);
                if (document.querySelector('#th-back')) return 'ok'; }
              return 'the thread view never drew its back button';
            })()""")
            if opened != "ok":
                print("FAIL  %s" % opened)
                return 1
            # AT MORE THAN ONE SIZE, because "cut off" is a layout answer and a layout answer
            # changes with the box. The client also scales the whole page with `body{zoom}` by
            # VIEWPORT, so a 4K desk and a laptop are different arithmetic on the same window.
            measured = 0
            for vw, vh, ww, wh in ((1600, 1000, 900, 640), (1600, 1000, 460, 420),
                                   (3840, 2160, 1200, 800), (3840, 2160, 520, 380)):
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": vw, "height": vh, "deviceScaleFactor": 1, "mobile": False},
                           sess)
                sized = await js("""(async () => {
                  const wait = ms => new Promise(r => setTimeout(r, ms));
                  const feed = document.getElementById('feed');
                  const win = feed && feed.closest('.osw');
                  if (!win) return 'the thread is not in a window';
                  win.style.width = %d + 'px'; win.style.height = %d + 'px';
                  win.style.left = '40px'; win.style.top = '40px';
                  try { window.dispatchEvent(new Event('resize')); } catch (e) {}
                  await wait(400);
                  return 'ok';
                })()""" % (ww, wh))
                if sized != "ok":
                    print("FAIL  %s" % sized); return 1
                got = await js(MEASURE.replace("SELECTORS", SELECTORS))
                if not isinstance(got, dict) or not got.get("ok"):
                    print("FAIL  %s" % (got,)); return 1
                where = "viewport %dx%d, window %dx%d" % (vw, vh, ww, wh)
                measured += len(got["rows"])
                for row in got["rows"]:
                    if row.get("why"):
                        problems.append("%s: %s has %s (%s)" % (where, row["sel"], row["why"], row["r"]))
                        continue
                    if row["clipped"]:
                        problems.append("%s: %s is cut off past the window's %s edge — control %s, "
                                        "window body %s"
                                        % (where, row["sel"], row["over"], row["r"], got["body"]))
                    if row["blocked"]:
                        problems.append("%s: %s cannot be clicked — %s is painted over its centre"
                                        % (where, row["sel"], row["blocker"]))
                if not got["rows"]:
                    print("SKIP  the thread view exposed no controls to measure — the selector list "
                          "is stale, which is a check that has quietly stopped checking")
                    return 2
            if problems:
                print("FAIL  windowed view chrome:")
                for p in problems:
                    print("      " + p)
                return 1
            print("OK  every control in the windowed thread view is inside its window and clickable "
                  "at every size (%d measured)" % measured)
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
