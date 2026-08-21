#!/usr/bin/env python3
"""One anatomy per post — audits a REAL timeline for cards that draw differently from their neighbours.

    "Posts on the timeline need uniformity: these three posts all display different ways."
    "when a repost is displayed, only the time of the original post is shown, but that AND when it
     was reposted [should be]."

The three posts in that report were three ordinary kind-1 notes, and measured in isolation their
cards were identical to the pixel. The differences only exist in a CROWD, which is why this check
loads a live feed and audits every card in it rather than rendering a fixture. On the load that
found the bug: 11 of 63 cards drew a two-line header and 4 of 4 reposts said "someone reposted".

  header-wrap       The name/handle/time row wrapped onto a second line, so this card is ~24px
                    taller than the identical card beside it. Decided by how long a stranger's
                    display name is, which is not a property of the post.
  unnamed-repost    A repost still reading "someone reposted" after the profiles have had time to
                    arrive. The name has to be a `.name[data-prof]` for decorateProfiles to fill;
                    baked-in escaped text can never be patched and stays "someone" for the session.
  repost-no-time    A repost header with no timestamp of its own. A repost is two events with two
                    times and the one that decides where it sits in your feed is the repost's.
  handle-overflow   A header element whose right edge is past the card — the failure mode of
                    forbidding the wrap without letting the flexible parts shrink.
  time-misaligned   Timestamps that do not share a right edge. They are what the eye uses to read
                    the column as a column.

Reposts are DATA, not layout: a feed that happens to carry none simply reports 0 and passes. That is
deliberate — a check that failed on a quiet timeline would be turned off.

    venv-unified/bin/python scripts/check_timeline_uniformity.py [base_url]

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / site unreachable).
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
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-tluniform-check"
# 1000px, not a real desktop width: at >=1024 the client lands on PosterChanOS's ICON GRID, which
# has no timeline in it at all (that screen is check_os_desktop.py's job). 1000px is classic mode —
# the widest layout that actually draws a feed, and the one where a header has the most room to
# misbehave without wrapping.
WIDTHS = [(390, 844, True), (360, 780, True), (1000, 900, False)]

AUDIT = r"""(() => {
  const out = {cards: 0, wrapped: [], unnamed: [], noTime: [], overflow: [], reposts: 0, rightEdges: {}};
  document.querySelectorAll('#tl-notes article.note').forEach(a => {
    const hd = a.querySelector('.hd'); if (!hd) return;      // a placeholder still loading its post
    out.cards++;
    const nm = hd.querySelector('.name'), tm = hd.querySelector('.time');
    const card = a.getBoundingClientRect();
    const who = ((nm && nm.textContent) || '').trim().slice(0, 30) || '(no name)';
    // WRAPPED is measured as "the name and the time are on different rows", not as a height
    // threshold — a two-line name would fail a height test while being perfectly uniform.
    if (nm && tm && Math.abs(nm.getBoundingClientRect().y - tm.getBoundingClientRect().y) > 3)
      out.wrapped.push(who);
    [nm, tm, hd.querySelector('.handle')].forEach(el => {
      if (!el) return;
      const b = el.getBoundingClientRect();
      if (b.width > 0 && b.right > card.right + 1)
        out.overflow.push(who + ' — ' + (el.className || el.tagName));
    });
    if (tm) { const r = Math.round(tm.getBoundingClientRect().right);
              out.rightEdges[r] = (out.rightEdges[r] || 0) + 1; }
    const rt = a.querySelector('.repost-tag');
    if (rt) {
      out.reposts++;
      if (/\bsomeone reposted\b/.test(rt.textContent || '')) out.unnamed.push(who);
      if (!rt.querySelector('.rt-when')) out.noTime.push(who);
    }
  });
  return out;
})()"""


async def run():
    if not shutil.which("google-chrome-stable"):
        print("SKIP  google-chrome-stable not installed")
        return 2
    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return await drive()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)


async def drive():
    import websockets
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

    problems = []
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        n = [0]

        async def call(method, params=None):
            n[0] += 1
            await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n[0]:
                    return msg.get("result")

        async def js(expr):
            r = await call("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True, "awaitPromise": True})
            if not r or r.get("exceptionDetails"):
                return None
            return r["result"].get("value")

        await call("Runtime.enable")
        await call("Page.enable")
        for w, h, mobile in WIDTHS:
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": w, "height": h, "deviceScaleFactor": 2 if mobile else 1,
                        "mobile": mobile})
            await call("Emulation.setTouchEmulationEnabled",
                       {"enabled": mobile, "maxTouchPoints": 5 if mobile else 0})
            await call("Page.navigate", {"url": f"{BASE}/client"})
            res = None
            for _ in range(30):                      # the feed is a relay round trip away
                await asyncio.sleep(1.5)
                res = await js(AUDIT)
                if res and res.get("cards", 0) >= 15:
                    break
            if res is None:
                print(f"SKIP  {w}px: page did not evaluate (site unreachable?)")
                return 2
            if res["cards"] < 15:
                print(f"SKIP  {w}px: only {res['cards']} cards drew (relay unreachable?)")
                return 2
            # A profile arriving is what turns "someone" into a name, so give the kind-0s a moment
            # before judging that one — otherwise this check fails on a slow relay rather than on a bug.
            await asyncio.sleep(6)
            res = await js(AUDIT) or res

            label = f"{w}px"
            for x in res["wrapped"]:
                problems.append((label, "header-wrap", x))
            for x in res["unnamed"]:
                problems.append((label, "unnamed-repost", x))
            for x in res["noTime"]:
                problems.append((label, "repost-no-time", x))
            for x in res["overflow"]:
                problems.append((label, "handle-overflow", x))
            edges = res["rightEdges"]
            if len(edges) > 1:
                problems.append((label, "time-misaligned",
                                 "timestamps end at " + ", ".join(f"{k}px×{v}" for k, v in edges.items())))
            print(f"{label}: cards={res['cards']} reposts={res['reposts']} "
                  f"wrapped={len(res['wrapped'])} unnamed={len(res['unnamed'])} "
                  f"noTime={len(res['noTime'])} overflow={len(res['overflow'])}")

    if not problems:
        print("OK  every card drew the same anatomy")
        return 0
    print()
    seen = set()
    for width, kind, detail in problems:
        key = (width, kind, detail)
        if key in seen:
            continue
        seen.add(key)
        print(f"FAIL  [{width}] {kind}: {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
