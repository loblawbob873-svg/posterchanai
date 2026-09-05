#!/usr/bin/env python3
"""The desktop app on WINDOWS and macOS: the bridge is there, the compositor is not.

    venv-unified/bin/python scripts/check_desktop_app_without_a_compositor.py [base_url]

WHY THIS SHAPE. `desktop/preload.js` injects `pcWM` on every platform -- its own comment promises
"absent rather than broken off a compositor", but what is absent is the ANSWER (`available()` says
no), not the object. `PCOSWin.enabled()` tested for the OBJECT, so in the plain desktop app on
Windows every PosterChan app opened as a real compositor toplevel that nothing could place, raise or
close, and its title bar's `pcWM.self()` rejected:

    "you can't maximize the Files Manager! pc:wm:self error"
    "all the posterchan apps are broken with that err on windows app"
    "clicking File Manager loads File Manager and Blossom"

`check_desktop_standalone.py` could not see it: its bridge stub has NO `pcWM` at all, so the branch
never ran. This one injects the bridge the way Windows really has it -- present, and answering no to
everything -- before any page script, and then uses the app.

Assertions:

  toplevels-offered   `PCOSWin.enabled()` is true with no compositor. Every window opened from then
                      on is unmanageable and, on Windows, uncloseable.
  compositor-error    A `pc:wm:*` call rejected into the page. That is the reported error, and on
                      this build it happens on an ordinary click.
  app-not-windowed    Opening an app produced no in-page window. With toplevels correctly refused,
                      the in-page frame is the whole UI.
  app-opened-twice    One click produced two surfaces -- the "File Manager loads File Manager and
                      Blossom" shape.
  noti-not-drawn      The bell drew nothing. `pcPopup` is injected on every platform, so testing for
  start-not-drawn     the object sent these down the compositor-window path on a machine with no
                      compositor -- and taking that branch is exactly what skips the in-page panel.
                      "taskbar, notifications, completely broken on windows app".
  maximise-dead       The window's maximise control does nothing and cannot: with no compositor it
                      must fall back to the window's own control.

Exit 0 clean * 1 problems * 2 could not run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://poster.place"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9494)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-nocomp-check"

# The bridge exactly as the desktop app has it off PosterChanOS: every method present, every answer
# "there is no window manager here". `available()` false is what the preload's own comment promises;
# the rest reject the way a missing socket does, because that is what the renderer actually sees.
BRIDGE = r"""
window.__pcWmErrors = [];
const _no = (name) => (...a) => {
  window.__pcWmErrors.push(name);
  return Promise.reject(new Error("Error invoking remote method 'pc:wm:" + name + "': no compositor"));
};
window.pcWM = {
  available: () => Promise.resolve(false),
  windows: _no('windows'), self: _no('self'), snap: _no('snap'), hide: _no('hide'),
  show: _no('show'), focus: _no('focus'), place: _no('place'), close: _no('close'),
  move: _no('move'), restore: _no('restore'), workArea: () => Promise.resolve(true),
  subscribe: () => Promise.resolve(false), on: () => {}, launch: _no('launch'),
  shellFront: () => Promise.resolve(true), preview: _no('preview'),
  control: (a) => { window.__pcWmControl = String(a || ''); return Promise.resolve(true); },
};
/* AND THE POPUP BRIDGE, which the preload also injects on every platform. Leaving it out is what
   made a first version of this check pass: without `pcPopup` the client draws its in-page panels,
   which is the very fallback the bug prevents. Present-and-refusing is the Windows shape. */
window.pcPopup = {
  open: _no('popup.open'), toggle: _no('popup.toggle'), close: _no('popup.close'),
  pick: _no('popup.pick'), act: _no('popup.act'),
};
window.__pcRejections = [];
window.addEventListener('unhandledrejection', e => {
  const m = String((e && e.reason && e.reason.message) || (e && e.reason) || '');
  if (m.indexOf('pc:wm:') >= 0 || m.indexOf('popup.') >= 0) window.__pcRejections.push(m);
});
"""

PROBE = r"""(async () => {
  if (!window.PCOSWin || !window.PCOS) return {skip: 'this build has no windowed desktop'};
  const out = {enabled: !!PCOSWin.enabled()};
  try { PCOS.enter(); } catch (e) { return {skip: 'enter() threw: ' + (e && e.message || e)}; }
  if (!PCOS.isOn()) return {skip: 'the desktop refused to open at this size'};
  await new Promise(r => setTimeout(r, 400));

  const count = () => document.querySelectorAll('.osw').length;
  const before = count();
  // The exact click that was reported: the Files/Blossom launcher entry.
  const icon = document.querySelector('.os-icon[data-view="blossom"], .os-icon[data-view="files"]')
            || document.querySelector('.os-icon[data-view]');
  if (!icon) return {skip: 'no desktop icons to open'};
  const view = icon.dataset.view;
  /* A SINGLE CLICK OPENS, and that is deliberate: "a double-click that does nothing the first time
     reads as broken" (os.js). A synthetic `dblclick` never fires `onclick`, so a first version of
     this probe reported that nothing opened -- a bug in the check, not the app. */
  icon.click();
  await new Promise(r => setTimeout(r, 1200));
  out.view = view;
  out.opened = count() - before;

  /* THE TASKBAR CONTROLS, which took the compositor-window path on the mere existence of the
     `pcPopup` bridge -- so on Windows the in-page panel was never drawn and no window could be
     placed either: "taskbar, notifications, completely broken on windows app". Each one must put
     something on screen HERE. */
  const panel = async (fire, sel) => {
    const before = document.querySelectorAll(sel).length;
    try { fire(); } catch (e) { return {threw: String(e && e.message || e)}; }
    await new Promise(r => setTimeout(r, 600));
    return {drawn: document.querySelectorAll(sel).length > before};
  };
  const bell = document.querySelector('#os-bell');
  const start = document.querySelector('#os-start');
  out.noti  = bell  ? await panel(() => bell.click(),  '.os-noti') : {skip: 1};
  if (bell) { bell.click(); await new Promise(r => setTimeout(r, 200)); }
  out.start = start ? await panel(() => start.click(), '.os-startmenu') : {skip: 1};
  if (start) { start.click(); await new Promise(r => setTimeout(r, 200)); }

  // And the control that failed: maximise, on whatever opened.
  const win = [...document.querySelectorAll('.osw')].pop();
  const max = win && win.querySelector('[data-w="max"]');
  const h0 = win ? win.offsetHeight : 0;
  if (max) { max.click(); await new Promise(r => setTimeout(r, 500)); }
  out.maximised = win ? (win.offsetHeight > h0 + 20 || win.classList.contains('max')) : false;
  out.hadMax = !!max;
  out.wmCalls = (window.__pcWmErrors || []).slice(0, 8);
  out.rejections = (window.__pcRejections || []).slice(0, 5);
  return out;
})()"""


async def drive(ws_url):
    import websockets
    problems = []
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        n = 0

        async def call(method, params=None):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n:
                    return msg.get("result", {})

        async def js(expr, awaited=False):
            r = await call("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
            return None if "exceptionDetails" in r else r["result"].get("value")

        await call("Runtime.enable")
        await call("Page.enable")
        # A preload runs before any page script; this is the CDP equivalent.
        await call("Page.addScriptToEvaluateOnNewDocument", {"source": BRIDGE})
        await call("Emulation.setDeviceMetricsOverride",
                   {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        await call("Page.navigate", {"url": BASE.rstrip("/") + "/client"})

        for _ in range(120):
            await asyncio.sleep(0.5)
            if await js("!!(window.__PC && window.PCOS)"):
                break
        else:
            print("SKIP  the client never finished loading")
            return 2
        await asyncio.sleep(3)

        r = await js(PROBE, awaited=True)
        if r is None or r.get("skip"):
            print("SKIP  " + ((r or {}).get("skip") or "the probe did not run"))
            return 2

        if r.get("enabled"):
            problems.append(("toplevels-offered",
                             "PCOSWin.enabled() is true with no compositor — every app would open "
                             "as a toplevel nothing can place, raise or close"))
        if r.get("rejections"):
            problems.append(("compositor-error",
                             f"a compositor call rejected into the page: {r['rejections'][0]}"))
        if r.get("opened", 0) < 1:
            problems.append(("app-not-windowed",
                             f"opening {r.get('view')!r} produced no in-page window; with toplevels "
                             "refused that frame is the entire UI"))
        elif r.get("opened", 0) > 1:
            problems.append(("app-opened-twice",
                             f"one click on {r.get('view')!r} produced {r['opened']} surfaces"))
        for name, label in (("noti", "the notification centre"), ("start", "the start menu")):
            got = r.get(name) or {}
            if got.get("skip"):
                continue
            if got.get("threw"):
                problems.append((f"{name}-threw", f"{label} threw: {got['threw']}"))
            elif not got.get("drawn"):
                problems.append((f"{name}-not-drawn",
                                 f"{label} drew nothing — with no compositor it must fall back to "
                                 f"the in-page panel, and taking the window path is what skips it"))
        if r.get("hadMax") and not r.get("maximised"):
            problems.append(("maximise-dead",
                             "the window's maximise control did nothing — with no compositor it "
                             "must fall back to the window's own control"))

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for code, why in problems:
            print(f"  - {code}: {why}")
        return 1
    print("OK    the desktop app behaves with no compositor: in-page windows, no pc:wm errors")
    return 0


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome")
    if not chrome:
        print("SKIP  no Chrome on this box")
        return 2
    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        return await drive(page["webSocketDebuggerUrl"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)


sys.exit(asyncio.run(main()))
