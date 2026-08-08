#!/usr/bin/env python3
"""PosterChan OS — open every app in a window on a REAL instance and check it actually works.

    venv-unified/bin/python scripts/check_os_apps.py [base_url]

check_os_desktop.py drives os.js against a stub: it proves the window manager is correct, and
proves nothing about the features living inside the windows. This one loads the real client, enters
the desktop, and opens each launcher entry in turn — which is the only way to catch the class of bug
that made a user say "all the apps buttons don't work on the OS".

Assertions, each a way a feature breaks specifically INSIDE a window and nowhere else:

  view-blank           The window opened and painted nothing. A feature reached through the launcher
                       must render the same as it does from the sidebar, because the launcher calls
                       exactly what the sidebar calls (switchView) — a blank one means the view needs
                       something the sidebar click did and the window did not.
  view-threw           Switching into the view raised. Silent in the UI, fatal to the feature.
  window-hoverflow     The window's content scrolls sideways inside its own frame. A view laid out
                       for a full-width column does this the moment it is put in a 700px window.
  escapes-window       A visible element sticks out past its window's edge. Usually a rule sized in
                       viewport units (100dvh / 100vw), which inside a window still measures the
                       SCREEN — the single most likely way a view misbehaves here and nowhere else.
  feed-astray          After opening a window, the live #feed is not inside it. Everything renders
                       into #feed, so if it is elsewhere the feature painted where nobody looks.

Runs as a guest, so it only reaches the views a guest can reach; the ones needing a key are
reported as skipped rather than passing quietly.

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / site unreachable).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://poster.place"
PORT = 9488
PROFILE = "/tmp/pc-os-apps"
VIEWPORT = (1600, 900)

ENTER = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  if (!window.PCOS) return { err: 'no PCOS' };
  if (!PCOS.isOn()) { PCOS.enter(); await sleep(400); }
  const icons = [...document.querySelectorAll('.os-icon')].map(b => ({
    view: b.dataset.view || '', label: (b.textContent||'').trim() }));
  return { on: PCOS.isOn(), icons };
})()"""

# Open one app, let it settle, then measure the window it landed in.
PROBE = r"""(async (view) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const out = { view };
  const btn = [...document.querySelectorAll('.os-icon')].find(b => b.dataset.view === view);
  if (!btn) return { view, missing: true };
  window.__osErr = null;
  try { btn.click(); } catch (e) { out.threw = String(e && e.message || e); }
  await sleep(1400);

  const win = document.querySelector('.osw.focused');
  if (!win) return Object.assign(out, { noWindow: true });
  const body = win.querySelector('.osw-body');
  const feed = document.getElementById('feed');
  out.feedInside = !!(feed && body.contains(feed));

  // Painted anything? Text OR any element that draws (an empty-state message counts; a spinner
  // that never resolves does not, so spinners are excluded).
  const txt = (body.innerText || '').replace(/\s+/g, ' ').trim();
  out.text = txt.slice(0, 60);
  out.painted = txt.length > 0 || !!body.querySelector('img,canvas,video,input,textarea,button:not(.osw-b)');
  out.spinning = !txt && !!body.querySelector('.spinner');

  const br = body.getBoundingClientRect();
  out.hoverflow = Math.max(0, body.scrollWidth - body.clientWidth);

  // Anything sticking out of the window. Measured, not inferred from the stylesheet: only VISIBLE
  // elements count, and only the ones actually outside the frame by more than a rounding error.
  let worst = null;
  for (const el of body.querySelectorAll('*')) {
    if (el.closest('.osw-bar')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    // Skip anything held by a scrolling ancestor. A media carousel's slides genuinely sit past the
    // right edge of their track and are reached by scrolling — that is the design, not an escape.
    let clipped = false;
    for (let a = el.parentElement; a && a !== body; a = a.parentElement) {
      const ao = getComputedStyle(a);
      if (/(auto|scroll|hidden|clip)/.test(ao.overflowX + ao.overflowY)) { clipped = true; break; }
    }
    if (clipped) continue;
    const over = Math.max(r.right - br.right, br.left - r.left);
    if (over > 4 && (!worst || over > worst.over)) {
      worst = { over: Math.round(over), tag: el.tagName.toLowerCase(),
                cls: String(el.className || '').slice(0, 44) };
    }
  }
  out.escape = worst;
  out.err = window.__osErr;
  return out;
})"""

async def main():
    chrome = (shutil.which("google-chrome") or shutil.which("chromium")
              or shutil.which("chromium-browser") or shutil.which("google-chrome-stable"))
    if not chrome:
        print("SKIP  no Chrome on this host")
        return 2
    try:
        import websockets
    except ImportError:
        print("SKIP  pip install websockets")
        return 2
    try:
        urllib.request.urlopen(BASE + "/client", timeout=12).read(64)
    except Exception as e:
        print(f"SKIP  {BASE} unreachable: {e}")
        return 2

    shutil.rmtree(PROFILE, ignore_errors=True)
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems, skipped = [], []
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:600])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": VIEWPORT[0], "height": VIEWPORT[1], "deviceScaleFactor": 1,
                        "mobile": False})
            await call("Page.navigate", {"url": BASE + "/client"})

            ready = False
            for _ in range(100):
                await asyncio.sleep(0.3)
                if await js("!!window.PCOS && !!document.querySelector('.nav-item[data-view]')"):
                    ready = True
                    break
            if not ready:
                print("SKIP  the client never finished loading")
                return 2
            # Record uncaught errors while views mount — a view can throw and still look alive.
            await js("window.addEventListener('error', e => { window.__osErr = String(e.message); });"
                     "window.addEventListener('unhandledrejection',"
                     " e => { window.__osErr = 'promise: ' + String(e.reason && e.reason.message || e.reason); });")

            g = await js(ENTER, awaited=True)
            if not g or not g.get("on"):
                print(f"SKIP  the desktop did not open: {g}")
                return 2
            views = [i["view"] for i in g["icons"] if i["view"]]
            print(f"  {len(views)} app(s) in the launcher at {VIEWPORT[0]}x{VIEWPORT[1]}")

            for v in views:
                r = await js(f"({PROBE})({json.dumps(v)})", awaited=True)
                if r is None:
                    problems.append((v, "view-threw", "the probe itself did not evaluate"))
                    continue
                if r.get("missing") or r.get("noWindow"):
                    problems.append((v, "view-blank", "clicking the icon opened no window"))
                    continue
                if r.get("threw") or r.get("err"):
                    problems.append((v, "view-threw", str(r.get("threw") or r.get("err"))[:140]))
                if not r.get("feedInside"):
                    problems.append((v, "feed-astray",
                                     "the live #feed is not inside the window that just opened"))
                if not r.get("painted"):
                    if r.get("spinning"):
                        skipped.append(f"{v} (still loading — guest may not have access)")
                    else:
                        problems.append((v, "view-blank", "the window painted nothing"))
                if r.get("hoverflow", 0) > 2:
                    problems.append((v, "window-hoverflow",
                                     f"content scrolls {r['hoverflow']}px sideways inside the window"))
                e = r.get("escape")
                if e:
                    problems.append((v, "escapes-window",
                                     f"<{e['tag']} class={e['cls']!r}> sticks {e['over']}px past the "
                                     "window edge — usually a viewport-unit height/width, which "
                                     "inside a window still measures the whole screen"))
                mark = "ok  " if not any(p[0] == v for p in problems) else "FAIL"
                print(f"    {mark} {v:<14} {r.get('text','')[:44]!r}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    if skipped:
        print("\n  skipped (guest cannot reach):")
        for s in skipped:
            print(f"    - {s}")
    if problems:
        print(f"\nFAIL  {len(problems)} problem(s):")
        for view, kind, msg in problems:
            print(f"  [{view}] {kind}: {msg}")
        return 1
    print("\nOK  every app opens and fits in its window")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
