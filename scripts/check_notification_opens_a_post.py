#!/usr/bin/env python3
"""Clicking a notification must open the post WITHOUT taking the desktop with it.

    venv-unified/bin/python scripts/check_notification_opens_a_post.py [base_url]

Reported: "I clicked on a notification on my laptop just now, and all the apps dissappear, then,
the post window is unscrollable." Two symptoms from one click, and they need not share a cause, so
they are asserted separately.

A notification row is performed in the SHELL, not in the 380px popup it was clicked in
(`pc:act:thread:<id>` -> os.js's tick router -> `PC().openThread(id)`). That is the exact call this
check makes, against the REAL client -- app.js and os.js together -- because the desktop half lives
in os.js and the thread half in app.js and the bug is in what they do to each other. The os.js unit
harness stubs `PC()`, so it cannot see this at all.

Assertions:

  windows-vanished   Windows that were open before the click are gone afterwards. On this desktop a
                     window is a DOM element that owns the shared `#feed` id when focused; anything
                     that re-renders the shell rather than opening a frame destroys the lot.
  no-post-window     The post did not get a window of its own -- i.e. it replaced the view behind
                     it, which is the same bug seen from the other side.
  post-unscrollable  The post window's body cannot be scrolled to the bottom of its own content. A
                     thread is usually taller than its frame; if the body is not a scroll container
                     the end of the conversation is unreachable and there is nothing on screen to
                     say so.
  feed-not-in-window The `#feed` id did not move into the new window. Every feature paints into
                     `#feed`, so if it stayed behind, the thread rendered into whatever was
                     previously focused -- which looks exactly like "the other windows are gone".

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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9491)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-noti-post-check"

# Enter the desktop and open two ordinary windows to stand in for "all the apps".
SETUP = r"""(async () => {
  if (!window.PCOS) return {skip: 'no PCOS on this build'};
  try { PCOS.enter(); } catch (e) { return {skip: 'enter() threw: ' + (e && e.message || e)}; }
  if (!PCOS.isOn()) return {skip: 'the desktop refused to open at this size'};
  const made = [];
  for (const [key, title] of [['probe:one', 'One'], ['probe:two', 'Two']]) {
    try {
      PCOS.openDoc(key, title, 'i-note', (host) => {
        // Enough content that a scroll container would have something to scroll.
        if (host) host.innerHTML = '<div style="height:4000px">' + title + '</div>';
      });
      made.push(key);
    } catch (e) { /* reported by the count below */ }
  }
  await new Promise(r => setTimeout(r, 300));
  return {opened: made.length, windows: document.querySelectorAll('.osw').length};
})()"""

# The exact call os.js's tick router makes for `pc:act:thread:<id>`.
CLICK = r"""(async (id) => {
  /* A window element carries no key -- its `view` lives in os.js's own `wins` array, not in the
     DOM -- so windows are identified by the TITLE the shell drew, which is the same thing a person
     is looking at. Getting this wrong the first time reported a bug that was not there. */
  const titles = () => [...document.querySelectorAll('.osw')]
    .map(w => (w.querySelector('.osw-title') || {}).textContent || '?');
  const before = titles();
  const api = window.__PC;
  if (!api || !api.openThread) return {skip: 'no openThread on this build'};
  try { api.openThread(id); } catch (e) { return {threw: String(e && e.message || e), before}; }
  await new Promise(r => setTimeout(r, 1500));
  const after = titles();
  const feed = document.getElementById('feed');
  const post = [...document.querySelectorAll('.osw')].find(
    w => ((w.querySelector('.osw-title') || {}).textContent || '') === 'Post');
  let scroll = null;
  if (post) {
    const body = post.querySelector('.osw-body') || post;
    // Ask the element to scroll and see whether it moved. `overflow` alone is not the question:
    // a container with overflow:auto and no height scrolls nowhere.
    const top0 = body.scrollTop;
    body.scrollTop = 10000;
    const moved = body.scrollTop > top0;
    body.scrollTop = top0;
    scroll = {overflowY: getComputedStyle(body).overflowY,
              scrollH: body.scrollHeight, clientH: body.clientHeight, moved: moved};
  }
  return {before, after, feedIn: !!(feed && post && post.contains(feed)),
          hasPost: !!post, scroll: scroll};
})"""


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

        async def js(expr, awaited=False, arg=None):
            src = expr if arg is None else f"({expr})({json.dumps(arg)})"
            r = await call("Runtime.evaluate",
                           {"expression": src, "returnByValue": True, "awaitPromise": awaited})
            if "exceptionDetails" in r:
                return None
            return r["result"].get("value")

        await call("Runtime.enable")
        await call("Page.enable")
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

        # A real post id off the timeline. Without one there is nothing to open.
        await asyncio.sleep(4)
        pid = await js("(document.querySelector('article.note[data-id]')||{}).dataset"
                       " && document.querySelector('article.note[data-id]').dataset.id || ''")
        if not pid:
            print("SKIP  no posts on the timeline to open")
            return 2

        setup = await js(SETUP, awaited=True)
        if not setup or setup.get("skip"):
            print("SKIP  " + ((setup or {}).get("skip") or "the desktop did not start"))
            return 2
        if setup.get("opened", 0) < 2:
            print("SKIP  could not open two windows to test with")
            return 2

        r = await js(CLICK, awaited=True, arg=pid)
        if r is None or r.get("skip"):
            print("SKIP  " + ((r or {}).get("skip") or "the click did not run"))
            return 2
        if r.get("threw"):
            problems.append(("windows-vanished", "openThread threw: " + r["threw"]))
        else:
            kept = [w for w in r["before"] if w in r["after"]]
            if len(kept) < len(r["before"]):
                gone = [w for w in r["before"] if w not in r["after"]]
                problems.append(("windows-vanished",
                                 f"{len(gone)} of {len(r['before'])} open windows disappeared when a "
                                 f"notification was opened: {gone}"))
            if not r.get("hasPost"):
                problems.append(("no-post-window",
                                 "the post did not open in a window of its own — it replaced the "
                                 "view behind it"))
            else:
                if not r.get("feedIn"):
                    problems.append(("feed-not-in-window",
                                     "#feed did not move into the post's window, so the thread "
                                     "painted into whatever was focused before"))
                s = r.get("scroll") or {}
                if s.get("scrollH", 0) > s.get("clientH", 0) + 4 and not s.get("moved"):
                    problems.append(("post-unscrollable",
                                     f"the post window's body will not scroll "
                                     f"(overflow-y:{s.get('overflowY')}, content {s.get('scrollH')}px "
                                     f"in a {s.get('clientH')}px frame)"))

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for code, why in problems:
            print(f"  - {code}: {why}")
        return 1
    print("OK    a notification opens the post in its own window, the desktop survives, it scrolls")
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
