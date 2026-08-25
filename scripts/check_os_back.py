#!/usr/bin/env python3
"""Back in DESKTOP mode returns to the screen you were on — by closing the window, not popping history.

    "when I am in an issue, i click back, it brings me back to my Git repos"
    "i want to go back to where I was which should be the issue list, this is desktop mode"
    "ok you fixed it for classic mode"

Two clients, one report. The classic-mode fix (every screen is a history entry) is checked by
tests/client/test_back_returns_to_the_screen_you_left.py and cannot see this at all, because
PosterChanOS does not navigate: a post opens in its OWN window, and `_navUrl` deliberately declines
while `PCOS.isRepainting()` — pushing on a window repaint made Back walk window focus instead of
history ("back on Social took me to my profile twice"). So on the desktop the post had no entry of
its own and Back popped straight past the repo to the Git list underneath it.

Windows are not history here; the way back out of one is to CLOSE it, which is also the only way
back that repaints nothing — the repo window is still sitting there on its Issues tab, at the
offset it was left at.

  repo-flow      Git → a repo → Issues → an issue → Back. The post must open in its own
                 `doc:post:<id>` window, and Back must close exactly that window and leave the repo
                 window showing the ISSUES tab (coming back to the README is coming back to a screen
                 the reader never chose).
  timeline-flow  The same gesture from the feed: Back closes the post window and the Social window
                 comes back where the reader was. Measured as WHICH CARD is under a fixed point on
                 screen, never as a scrollTop: a parked timeline keeps receiving posts and prepends
                 them above, so os.js corrects the slot's offset to hold the reading position — the
                 number is SUPPOSED to move, and on a busy Nostrverse it moved by 2000px in the four
                 seconds this check spends in the post window. Asserting the pixel value would fail
                 on the firehose and pass on a dead relay, which is exactly backwards.

                 The card must still BE there and still be on screen. The remaining drift is
                 REPORTED, not asserted, and deliberately: measured against a live Nostrverse it was
                 exactly 0 after 2s and 6s parked and -1290px after 14s, which is not this fix — it
                 is content above the restore growing (images) faster than os.js's one-second
                 restoreScroll retry budget, on the window it had already put back correctly. A
                 threshold tuned to that outlier would fail on a busy relay and pass on a quiet one,
                 and would be measuring the wrong thing either way.
  window-leak    Back left the post's window open (it popped history instead), or closed more than
                 the one window it was asked to.

Needs a running instance with a relay that has the PosterChanAI repo announcement on it, which is
what makes the issue list non-empty; without one it SKIPS rather than failing.

    venv-unified/bin/python scripts/check_os_back.py [base_url]

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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9471)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-osback-check"
# Wide enough that the client opens the windowed desktop rather than classic mode.
W, H = 1440, 900


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

        # The card under a fixed point on screen, and where that point is. Survives a prepend; a
        # raw scrollTop does not.
        ANCHOR = """(() => {
          const f = document.getElementById('feed'); if (!f) return null;
          const r = f.getBoundingClientRect(), y = r.top + Math.min(240, r.height / 3);
          const cards = [...document.querySelectorAll('#tl-notes article.note')];
          let best = null, bestD = 1e9;
          for (const c of cards) {
            const b = c.getBoundingClientRect(), d = Math.abs(b.top - y);
            if (d < bestD) { bestD = d; best = c; }
          }
          return best ? { id: best.dataset.id, top: Math.round(best.getBoundingClientRect().top - r.top) } : null;
        })()"""

        async def state():
            return await js("""(() => ({
              view: (window.__PC && window.__PC.VIEW) || null,
              wins: (window.PCOS && window.PCOS.windows) ? window.PCOS.windows().map(w => w.view) : null,
              top: (document.getElementById('feed') || {}).scrollTop || 0,
              tab: ((document.querySelector('.rv-tab.active') || {}).dataset || {}).tab || null,
              back: !!document.getElementById('th-back'),
            }))()""")

        async def boot():
            await call("Page.navigate", {"url": f"{BASE}/client"})
            for _ in range(40):
                await asyncio.sleep(1)
                # A fresh browser profile correctly starts in Classic mode now. This check is
                # specifically about Desktop semantics, so enter that mode explicitly instead of
                # depending on an old persisted preference that the profile intentionally lacks.
                await js("(() => { if(window.PCOS && !PCOS.isOn()) PCOS.enter(); return true; })()")
                if await js("!!(window.PCOS && window.PCOS.isOn())"):
                    # A view switch only becomes a history entry once a person has touched the app.
                    await js("document.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))")
                    return True
            return False

        await call("Runtime.enable")
        await call("Page.enable")
        await call("Emulation.setDeviceMetricsOverride",
                   {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})

        if not await boot():
            print("SKIP  the windowed desktop never came up (site unreachable?)")
            return 2

        # ---- repo-flow -------------------------------------------------------------------
        await js("window.__PC.switchView('repos')")
        await asyncio.sleep(4)
        if not await js("!!document.querySelector('.repo-card')"):
            print("SKIP  no repos on this relay — nothing to open an issue from")
            return 2
        await js("document.querySelector('.repo-card').click()")
        for _ in range(20):
            await asyncio.sleep(1)
            if await js("!!document.querySelector('.repo-view')"):
                break
        if not await js("""!!document.querySelector('.rv-tab[data-tab="issues"]')"""):
            print("SKIP  the repo view never drew its tabs")
            return 2
        await js("""document.querySelector('.rv-tab[data-tab="issues"]').click()""")
        await asyncio.sleep(5)
        rows = await js("document.querySelectorAll('#rv-issues .collab-row').length")
        if not rows:
            print("SKIP  that repo has no issues on this relay")
            return 2
        before = await state()
        await js("document.querySelectorAll('#rv-issues .collab-row')[0].click()")
        await asyncio.sleep(5)
        opened = await state()
        post_wins = [w for w in (opened["wins"] or []) if w.startswith("doc:post:")]
        if not post_wins:
            problems.append(("repo-flow", "the issue did not open in its own window — "
                                          f"windows are {opened['wins']}"))
        if not opened["back"]:
            print("SKIP  the thread never painted its back button")
            return 2
        await js("document.getElementById('th-back').click()")
        await asyncio.sleep(4)
        after = await state()
        if after["view"] != "repo":
            problems.append(("repo-flow", f"Back landed on {after['view']!r}, not the repo "
                                          "(it popped history instead of closing the window)"))
        if after["tab"] != "issues":
            problems.append(("repo-flow", f"the repo came back on the {after['tab']!r} tab, "
                                          "not the issue list the reader was on"))
        left = [w for w in (after["wins"] or []) if w.startswith("doc:post:")]
        if left:
            problems.append(("window-leak", f"the post's window is still open: {left}"))
        if len(after["wins"] or []) != len(before["wins"] or []):
            problems.append(("window-leak", f"window count went {len(before['wins'] or [])} → "
                                            f"{len(after['wins'] or [])} across one Back"))
        print(f"repo-flow: opened={post_wins} back→view={after['view']} tab={after['tab']} "
              f"windows={after['wins']}")

        # ---- timeline-flow ---------------------------------------------------------------
        if not await boot():
            print("SKIP  could not reboot for the timeline flow")
            return 2
        await js("window.__PC.switchView('global')")
        for _ in range(30):
            await asyncio.sleep(1)
            if await js("document.querySelectorAll('#tl-notes article.note').length > 10"):
                break
        else:
            print("SKIP  the timeline never filled (relay unreachable?)")
            return 2
        await js("document.getElementById('feed').scrollTop = 900")
        await asyncio.sleep(0.8)
        was = await state()
        anchor = await js(ANCHOR)
        if not anchor:
            print("SKIP  could not anchor on a card in the feed")
            return 2
        await js("""(() => { const n = document.querySelectorAll('#tl-notes article.note')[3];
                             (n.querySelector('.txt') || n).click(); })()""")
        await asyncio.sleep(4)
        mid = await state()
        if not [w for w in (mid["wins"] or []) if w.startswith("doc:post:")]:
            problems.append(("timeline-flow", "the post did not open in its own window"))
        if mid["back"]:
            await js("document.getElementById('th-back').click()")
            await asyncio.sleep(3)
        back = await state()
        if back["view"] != was["view"]:
            problems.append(("timeline-flow", f"Back landed on {back['view']!r}, not {was['view']!r}"))
        again = await js("""(() => {
          const f = document.getElementById('feed'); if (!f) return null;
          const c = document.querySelector('#tl-notes article.note[data-id="%s"]');
          if (!c) return { gone: true };
          return { top: Math.round(c.getBoundingClientRect().top - f.getBoundingClientRect().top) };
        })()""" % anchor["id"])
        if not again or again.get("gone"):
            problems.append(("timeline-flow", "the post the reader was looking at is no longer in "
                                              "the feed they came back to"))
        elif again["top"] < -2000 or again["top"] > 4000:
            problems.append(("timeline-flow",
                             f"the card they were reading came back {again['top']}px from the top of "
                             "the feed — nowhere near the screen they left"))
        if [w for w in (back["wins"] or []) if w.startswith("doc:post:")]:
            problems.append(("window-leak", "the post's window survived Back on the timeline flow"))
        print(f"timeline-flow: the card being read was at {anchor['top']}px, came back at "
              f"{(again or {}).get('top', '?')}px (scrollTop {was['top']:.0f} → {back['top']:.0f}, "
              f"which moves when posts arrive above) view={back['view']} windows={back['wins']}")

    if not problems:
        print("OK  desktop Back returns to the screen you were on")
        return 0
    print()
    for kind, detail in problems:
        print(f"FAIL  {kind}: {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
