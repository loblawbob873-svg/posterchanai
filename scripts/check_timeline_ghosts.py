#!/usr/bin/env python3
"""The "ghost timeline" check: a reply card must never be a label with no post under it.

Reported from the Android app as "you only see REPLYING TO ... and no actual posts or replies" —
a screen of `↩ REPLYING TO alice` headers with nothing between them. The header is built from the
reply's OWN tags, so it renders whatever else fails; that is what makes the failure look like an
empty feed rather than an error, and why it survived three earlier fixes aimed at the same picture.

Two assertions, because there are two ways to get that screen and they need different fixes:

  intact       every .reply-pair on a real timeline holds an <article class="note">. This is the
               regression test for feedNoteHtml, which now builds the CARD first and only wraps a
               label around one that exists.
  self-heals   plant a ghost (strip the article out of a pair, exactly as the bug leaves it) and the
               sweep puts the post back. The timeline's reconcile matches on data-key and REUSES the
               card already on screen, so without this sweep a pair that came out wrong stays wrong
               for the life of the page — which is the half of the report that says a reload fixes it.

Runs as a GUEST against a real instance (the global feed is full of replies), so it needs Chrome and
a reachable node:

    venv-unified/bin/python scripts/check_timeline_ghosts.py [base_url]

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / no websockets / no replies
to test against).
"""
import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://poster.place"
PORT = 9483
PROFILE = "/tmp/pc-ghost-check"

# Every reply card on screen, and whether its post half is there and drew anything.
SURVEY = r"""(() => {
  const pairs = [...document.querySelectorAll('#tl-notes .reply-pair')];
  return {
    pairs: pairs.length,
    notes: document.querySelectorAll('#tl-notes article.note').length,
    ghosts: pairs.filter(p => !p.querySelector('article.note'))
                 .map(p => ({key: p.dataset.key || '?', html: p.innerHTML.slice(0, 200)})),
    blank: pairs.filter(p => { const a = p.querySelector('article.note');
                               return a && p.offsetHeight > 0 && a.offsetHeight === 0; }).length,
    /* THE THIRD SHAPE: present, full height, and transparent — what `anim-off` used to do to every
       card drawn while the app was backgrounded, and the one neither probe above can see. Measured
       as opacity on the cards that HAVE a box, so a post mid-entry-animation is not counted. */
    invisible: pairs.filter(p => { const a = p.querySelector('article.note');
                                   return a && a.offsetHeight > 0
                                          && parseFloat(getComputedStyle(a).opacity) === 0; }).length,
    animOff: document.body.classList.contains('anim-off'),
    hidden: document.hidden,
  };
})()"""

# Plant one, sweep, report. Returns the state before/after so a partial fix can't read as a pass.
PLANT = r"""(() => {
  const pair = document.querySelector('#tl-notes .reply-pair');
  if (!pair) return {skip: 'no reply cards on this timeline'};
  const key = pair.dataset.key || '';
  const art = pair.querySelector('article.note');
  if (!art) return {skip: 'the first reply card is already a ghost'};
  art.remove();                                    // <- exactly what the bug leaves behind
  const before = !!document.querySelector('#tl-notes .reply-pair[data-key="' + key + '"] article.note')
              || !!document.querySelector('#tl-notes article.note[data-id="' + key + '"]');
  // measure:true — the same call _drawTimeline makes, so the zero-height half of the sweep is
  // exercised here too rather than only the missing-article half.
  const fixed = window.__PC && window.__PC.healGhostPairs
              ? window.__PC.healGhostPairs(document.getElementById('tl-notes'), true) : -1;
  // The sweep replaces the whole pair, so look for the card by its event id rather than inside the
  // element we planted into — that element is gone on success.
  const after = !!document.querySelector('#tl-notes article.note[data-id="' + key + '"]');
  return {key: key, before: before, after: after, fixed: fixed,
          stats: window.__PC && window.__PC.ghostStats ? window.__PC.ghostStats() : null};
})()"""


async def drive(ws_url):
    import websockets
    problems = []
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        n = [0]

        async def call(method, params=None):
            n[0] += 1
            await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == n[0]:
                    if "error" in m:
                        raise RuntimeError(m["error"])
                    return m.get("result", {})

        async def js(expr):
            r = await call("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True, "awaitPromise": True})
            return r.get("result", {}).get("value")

        await call("Page.enable")
        await call("Runtime.enable")
        await call("Page.navigate", {"url": BASE + "/client"})

        survey = None
        for _ in range(10):                       # the feed fills from the relay; give it time
            await asyncio.sleep(6)
            survey = await js(SURVEY)
            if survey and survey.get("pairs"):
                break
        if not survey or not survey.get("pairs"):
            print("SKIP  no reply cards on the global feed to check against")
            return 2

        print(f"      {survey['pairs']} reply cards, {survey['notes']} posts on screen")
        for g in survey["ghosts"]:
            problems.append(f"ghost reply card (no post under the label): {g['key']} — {g['html']!r}")
        if survey["blank"]:
            problems.append(f"{survey['blank']} reply card(s) drew a zero-height post")
        if survey.get("invisible"):
            problems.append(
                f"{survey['invisible']} reply card(s) are present, full height and TRANSPARENT — "
                "the post is drawn and cannot be seen, which reads as a label with nothing under it")
        if survey.get("animOff") and not survey.get("hidden"):
            problems.append(
                "body.anim-off is set on a VISIBLE page — see _animOff in app.js. While that class "
                "is on, anything with an entry animation used to be frozen at opacity 0")

        planted = await js(PLANT)
        if not planted:
            print("SKIP  the page returned nothing for the plant step")
            return 2
        if planted.get("skip"):
            print("SKIP  " + planted["skip"])
            return 2
        if planted.get("fixed") == -1:
            problems.append("PC.healGhostPairs is not exposed — the ghost sweep cannot be verified")
        elif planted.get("before"):
            problems.append("could not plant a ghost — the check itself is broken, not the app")
        elif not planted.get("after"):
            problems.append("a planted ghost was NOT healed: a reply card whose post goes missing "
                            f"stays a bare label (sweep fixed {planted.get('fixed')})")
        else:
            print(f"      planted ghost healed (sweep fixed {planted.get('fixed')}, "
                  f"stats {planted.get('stats')})")

    if problems:
        print("\nFAIL")
        for p in problems:
            print("  - " + p)
        return 1
    print("\nOK  no ghost reply cards, and a planted one heals")
    return 0


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    if not shutil.which("google-chrome-stable"):
        print("SKIP  no Chrome on this box")
        return 2

    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
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
