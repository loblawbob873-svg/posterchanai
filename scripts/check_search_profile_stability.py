#!/usr/bin/env python3
"""Timelines, Profiles and Search — measured as a RATE over many fresh sessions, not once.

    venv-unified/bin/python scripts/check_search_profile_stability.py [sessions] [base_url]

WHY A RATE, AND WHY FRESH SESSIONS. All three of these were reported as "unstable", and a single-shot
check is the wrong instrument for that: run each flow once and it passes, which is exactly what kept
them open across several rounds of "fixed". Run the FIRST search of ten separate sessions and the
shape appears at once — measured before the fix, #1 came back `complete === false` with 0 posts while
#2 through #10 returned 40 every time. "The first one fails" is invisible from a warm page, so every
trial here boots its own browser session with a throwaway key.

THE THREE FLOWS, and the exact failure each one exists to catch:

  timeline  Cards must be PRESENT and VISIBLE. Those are different questions, and the difference is
            the Android "empty timeline with REPLYING TO..." bug: `body.anim-off` (set whenever the
            app is backgrounded) used to FREEZE `.note`'s entry animation at its opacity-0 keyframe,
            so cards sat in the DOM at full height and drew nothing, while `.reply-ctx` — which has no
            animation — rendered perfectly above them. So this asserts on computed opacity, replays
            the resume path (anim-off on, draw, off), and counts ghost pairs (a "replying to" label
            with no card under it) which must be zero.

  profile   Cold-load /client/<npub> and require the three things that die TOGETHER when the render
            aborts halfway: the posts, the tab row, and a BOUND Copy-npub. That trio is the report
            "no posts, hamburger doesn't work, can't copy npub" — one broken render, three symptoms
            that look like three unrelated bugs. Asserting only on posts would miss the bindings.

  search    Boot, run ONE search, require actual posts. A truthful "your relays didn't answer" still
            FAILS here on purpose: the user does not care that the message was honest, they care that
            there were 40 results and they were shown none.

Exit 0 = every trial passed, 1 = any failed, 2 = could not run (no Chrome / no websockets).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

PORT = 9499
PROFILE_DIR = "/tmp/pc-stability-check"
SESSIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
BASE = sys.argv[2] if len(sys.argv) > 2 else "https://poster.place"
# A throwaway identity: these flows must work for a logged-in user, and a guest exercises different
# branches (no follows, no mutes, no pinned posts).
SK = os.urandom(32).hex()
NPUB = "npub1fdtthaqujtjcd6yfy7kt0zpkadyl9vvypq00s5nztnmche74d0tqv6uwwr"
# A REPLY-HEAVY AUTHOR, and the reason this second npub exists: the Posts tab excludes replies, so an
# account whose recent window is all replies takes a DIFFERENT path — a background backfill that pages
# further back looking for top-level posts. Every profile in the original check had posts on page one,
# so that path never ran in any trial, and a change to it shipped a regression the suite called 9/9.
# The assertion here is not "it finds posts" (it may genuinely have none recently) but that the render
# COMPLETES: the tab row and a BOUND Copy-npub, which is what dies when a profile aborts half-built.
NPUB_REPLIES = "npub1zl9wau3w0n0ll9kqycrkkehegtd4pcq8sgal9kefw7fn7r7956vqyv9gnh"
QUERIES = ["half-life", "nostr", "bitcoin", "doom", "music", "art", "linux", "photography"]

# Present is not the same as visible. Reads computed opacity on the cards themselves, and counts the
# two shapes of ghost: a reply label with no <article>, and an <article> nobody can see.
TIMELINE_PROBE = r"""(() => {
  const cards = [...document.querySelectorAll('#tl-notes article.note')];
  // STUCK, not merely mid-fade. `.note` animates opacity 0 -> 1 over .3s, so a card that arrived a
  // moment ago is legitimately transparent right now; counting it made this check fail on a busy
  // feed for the one reason that is not a bug. A card is only wrong if it is transparent AND has no
  // running animation to finish — which is exactly the anim-off freeze this exists to catch.
  const invisible = cards.filter(c => {
    const st = getComputedStyle(c);
    if (parseFloat(st.opacity) !== 0) return false;
    return st.animationName === 'none' || st.animationPlayState === 'paused';
  }).length;
  const zero = cards.filter(c => c.offsetHeight === 0).length;
  const pairs = [...document.querySelectorAll('#tl-notes .reply-pair')];
  return {cards: cards.length, invisible, zero,
          labelWithNoCard: pairs.filter(p => !p.querySelector('article.note')).length};
})()"""


async def _session(ws, flow, query):
    import websockets
    async with websockets.connect(ws, max_size=64 * 1024 * 1024) as w:
        n = [0]

        async def call(method, params=None):
            n[0] += 1
            await w.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                r = json.loads(await w.recv())
                if r.get("id") == n[0]:
                    if "error" in r:
                        raise RuntimeError(r["error"])
                    return r.get("result", {})

        async def js(expr):
            r = await call("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True, "awaitPromise": True})
            return r.get("result", {}).get("value")

        await call("Page.enable")
        await call("Runtime.enable")
        # Phone metrics: this is the surface all three were reported on.
        await call("Emulation.setDeviceMetricsOverride",
                   {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
        await call("Page.addScriptToEvaluateOnNewDocument",
                   {"source": "try{localStorage.setItem('pc_nostr_session',JSON.stringify({sk:%s}));}catch(e){}"
                              % json.dumps(SK)})

        if flow in ("profile", "profile_replies"):
            who = NPUB_REPLIES if flow == "profile_replies" else NPUB
            await call("Page.navigate", {"url": f"{BASE}/client/{who}"})
            await asyncio.sleep(20)
            st = await js("""JSON.stringify({
                posts: document.querySelectorAll('#prof-list article.note').length,
                tabs:  document.querySelectorAll('.prof-tab').length,
                copy:  !!(document.getElementById('copy-npub')||{}).onclick,
                title: (document.getElementById('view-title')||{}).textContent})""")
            d = json.loads(st)
            if flow == "profile_replies":
                # Posts may legitimately be 0 here. What must NOT happen is the render aborting
                # before its bindings — that is the reported "no posts, dead hamburger, dead copy".
                return (d.get("tabs") == 5 and d.get("copy")
                        and d.get("title") == "Profile"), st
            return (d.get("posts", 0) > 0 and d.get("tabs") == 5 and d.get("copy")), st

        await call("Page.navigate", {"url": BASE + "/client"})
        for _ in range(80):
            await asyncio.sleep(0.4)
            if await js("!!window.__PC && document.querySelectorAll('#tl-notes article.note').length>2"):
                break

        if flow == "timeline":
            # Let the entry animations FINISH before measuring. `.note` animates opacity 0 -> 1 over
            # .3s, so a probe fired during a fresh draw reads every card as invisible and the check
            # fails for the one reason that is not a bug. Settle, then measure.
            await asyncio.sleep(2.0)
            before = await js("JSON.stringify(%s)" % TIMELINE_PROBE)
            # Replay the resume path: the class the app sets while backgrounded, a draw underneath it,
            # then the class off. A card drawn while it is on must still be visible afterwards.
            await js("document.body.classList.add('anim-off')")
            # Insert and MEASURE IN THE SAME EVALUATION. The timeline reconcile rebuilds #tl-notes
            # from the Store and drops anything it did not put there, so a probe measured a second
            # later has usually been swept — which reads as `opacity:null` and fails for the wrong
            # reason. What is under test is the STYLE a card gets while anim-off is on, and that is
            # resolved synchronously by getComputedStyle.
            during = await js("""(()=>{const b=document.getElementById('tl-notes');
                if(!b) return JSON.stringify({opacity:null, why:'no #tl-notes'});
                const a=document.createElement('article'); a.className='note';
                a.innerHTML='<div class="body">resume probe</div>'; b.prepend(a);
                const op=getComputedStyle(a).opacity, h=a.offsetHeight; a.remove();
                return JSON.stringify({opacity:op, height:h});})()""")
            await js("document.body.classList.remove('anim-off')")
            await asyncio.sleep(0.6)
            after = await js("JSON.stringify(%s)" % TIMELINE_PROBE)
            b, d, a = json.loads(before), json.loads(during), json.loads(after)
            ok = (b["cards"] > 0 and b["invisible"] == 0 and b["labelWithNoCard"] == 0
                  and d["opacity"] not in (None, "0") and a["invisible"] == 0
                  and a["labelWithNoCard"] == 0)
            return ok, f'boot={before} backgrounded={during} after={after}'

        await js(f"__PC.runSearch({json.dumps(query)})")
        await asyncio.sleep(14)
        st = await js("""JSON.stringify({
            posts: document.querySelectorAll('#search-posts article.note').length,
            retry: !!document.getElementById('search-retry'),
            title: (document.getElementById('view-title')||{}).textContent})""")
        d = json.loads(st)
        return (d.get("title") == "Search" and d.get("posts", 0) > 0), st


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    if not shutil.which("google-chrome-stable"):
        print("SKIP  no Chrome on this box")
        return 2

    tally = {"timeline": [0, 0], "profile": [0, 0], "profile_replies": [0, 0], "search": [0, 0]}
    for i in range(SESSIONS):
        for flow in ("timeline", "profile", "profile_replies", "search"):
            shutil.rmtree(PROFILE_DIR, ignore_errors=True)
            proc = subprocess.Popen(
                ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
                 f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE_DIR}", "about:blank"],
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
                ok, detail = await _session(page["webSocketDebuggerUrl"], flow,
                                            QUERIES[i % len(QUERIES)])
                tally[flow][0] += 1 if ok else 0
                tally[flow][1] += 1
                print(f"  {flow:<9} session {i+1}/{SESSIONS}: {'ok  ' if ok else 'FAIL'} {detail}")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    print()
    failed = 0
    for flow, (good, total) in tally.items():
        print(f"  {flow}: {good}/{total} passed")
        failed += total - good
    if failed:
        print("\nFAIL — see the trials above")
        return 1
    print("\nOK  timelines, profiles and search were stable across every fresh session")
    return 0


sys.exit(asyncio.run(main()))
