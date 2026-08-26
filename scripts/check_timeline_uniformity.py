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
  header-stub       A truncated name or handle with almost nothing left of it. Measured: making the
                    handle give way twice as fast as the name produced `s…` beside a long display
                    name — a one-character stub, worse than the wrap it replaced.
  name-loses        The NAME truncated while the handle beside it did not, and the handle is the
                    wider of the two: the identity gave way so the address could be spelled out
                    (`bitcoinl…` beside `bitcoinlimit@verified-nost…`).
  time-misaligned   Timestamps that do not share a right edge. They are what the eye uses to read
                    the column as a column.

Reposts are DATA, not layout: a feed that happens to carry none simply reports 0 and passes. That is
deliberate — a check that failed on a quiet timeline would be turned off.

THE WORST CASE IS PLANTED, NOT WAITED FOR. Everything above audits whoever happens to be posting,
and the first version of this check went green against a bad rule simply because that minute's feed
had no cruelly long display name in it. A pass that depends on the crowd is not a pass, so the audit
also clones one real card and overwrites its name and handle with known extremes — a 63-character
npub (an author whose kind-0 has not arrived), a long human name beside a long nip05, and a short
name beside a long one. Those three are measured under the real stylesheet at every width, and they
are removed again before the next.

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
  const out = {cards: 0, wrapped: [], unnamed: [], noTime: [], overflow: [], stubs: [],
               nameLoses: [], reposts: 0, rightEdges: {}};
  document.querySelectorAll('#tl-notes article.note').forEach(a => {
    const hd = a.querySelector('.hd'); if (!hd) return;      // a placeholder still loading its post
    out.cards++;
    const nm = hd.querySelector('.name'), tm = hd.querySelector('.time');
    const card = a.getBoundingClientRect();
    const who = ((nm && nm.textContent) || '').trim().slice(0, 30) || '(no name)';
    // WRAPPED is measured as "the name and the time are on different rows", not as a height
    // threshold — a two-line name would fail a height test while being perfectly uniform.
    const nb = nm && nm.getBoundingClientRect(), tb = tm && tm.getBoundingClientRect();
    // Custom emoji can make the name's inline box a few pixels taller than plain timestamp text
    // even though both still occupy the same row. A real wrap separates their vertical ranges.
    if (nb && tb && (nb.bottom < tb.top - 3 || tb.bottom < nb.top - 3))
      out.wrapped.push(who + ' — ' + Math.round(nb.y) + '/' +
                       Math.round(tb.y) + ' — ' +
                       String(hd.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100));
    [nm, tm, hd.querySelector('.handle')].forEach(el => {
      if (!el) return;
      const b = el.getBoundingClientRect();
      if (b.width > 0 && b.right > card.right + 1)
        out.overflow.push(who + ' — ' + (el.className || el.tagName));
    });
    if (tm) { const r = Math.round(tm.getBoundingClientRect().right);
              out.rightEdges[r] = (out.rightEdges[r] || 0) + 1; }
    // A stub is measured in PIXELS OF SURVIVING TEXT, not in characters: the ellipsis is drawn by
    // the engine and the text node still holds the whole string, so textContent cannot see this.
    const hl = hd.querySelector('.handle');
    const cut = el => !!el && el.scrollWidth > el.clientWidth + 1;
    [[nm, 'name'], [hl, 'handle']].forEach(([el, what]) => {
      if (cut(el) && el.getBoundingClientRect().width < 40)
        out.stubs.push(who + ' — ' + what + ' cut to ' + Math.round(el.getBoundingClientRect().width) + 'px');
    });
    /* The pathology is not "the name gave way" — with the handle capped rather than shrinkable, a
       very long name giving up some of itself is the rule working. It is the name reduced to a
       fraction of the address beside it: `bitcoinl…` (77px) next to `bitcoinlimit@verified-nost…`
       (172px). Two thirds is the line, and it is measured in pixels because the ellipsis is the
       engine's — textContent still holds the whole string. */
    if (cut(nm) && hl && !cut(hl)) {
      const nw = nm.getBoundingClientRect().width, hw = hl.getBoundingClientRect().width;
      if (nw < hw * 0.66)
        out.nameLoses.push(who + ' — name cut to ' + Math.round(nw) + 'px beside a ' +
                           Math.round(hw) + 'px handle kept in full');
    }
    if (a.dataset.pcPlanted) out.planted = (out.planted || 0) + 1;
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

        # Three headers the feed may never produce on its own, measured under the real stylesheet.
        # The name/handle pairs are the shapes that actually broke: an un-resolved npub, a long human
        # name beside a long nip05, and a short name that a long address can crowd out.
        PLANT = r"""(() => {
          const src = document.querySelector('#tl-notes article.note');
          if (!src) return 0;
          const CASES = [
            ['npub1twanjtp3mr0ha65uzhug5xvr9vuh6h2gp52pau2rlxy5ta29qqwst5xjqh', '@17mugz59'],
            ['Jay Blue Ribbon, Spiritual Advisor', 'spiritualadvisor@nostrcheck.me'],
            ['bitcoinlimit', 'bitcoinlimit@verified-nostr.example.com'],
          ];
          CASES.forEach(([n, h]) => {
            const c = src.cloneNode(true);
            c.dataset.pcPlanted = '1';
            const nm = c.querySelector('.name'), hl = c.querySelector('.handle');
            if (!nm || !hl) return;
            nm.textContent = n; hl.textContent = h;
            src.parentNode.insertBefore(c, src);
          });
          return document.querySelectorAll('#tl-notes [data-pc-planted]').length;
        })()"""
        UNPLANT = "document.querySelectorAll('#tl-notes [data-pc-planted]').forEach(e=>e.remove()); 1"

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
            # One real card is sufficient as the DOM/CSS template: the cruel long-name cases are
            # cloned below, and reposts are explicitly optional. Requiring 15 made this visual gate
            # skip on a healthy but quiet/filtered relay (six cards were fully rendered), turning
            # current relay traffic into a release prerequisite without adding any coverage.
            if res["cards"] < 1:
                print(f"SKIP  {w}px: no timeline card drew (relay unreachable?)")
                return 2
            # A profile arriving is what turns "someone" into a name, so give the kind-0s a moment
            # before judging that one — otherwise this check fails on a slow relay rather than on a bug.
            await asyncio.sleep(6)
            planted = await js(PLANT)
            res = await js(AUDIT) or res
            await js(UNPLANT)
            if not planted:
                print(f"SKIP  {w}px: could not plant the worst-case headers")
                return 2

            label = f"{w}px"
            for x in res["wrapped"]:
                problems.append((label, "header-wrap", x))
            for x in res["unnamed"]:
                problems.append((label, "unnamed-repost", x))
            for x in res["noTime"]:
                problems.append((label, "repost-no-time", x))
            for x in res["overflow"]:
                problems.append((label, "handle-overflow", x))
            for x in res["stubs"]:
                problems.append((label, "header-stub", x))
            for x in res["nameLoses"]:
                problems.append((label, "name-loses", x))
            edges = res["rightEdges"]
            if len(edges) > 1:
                problems.append((label, "time-misaligned",
                                 "timestamps end at " + ", ".join(f"{k}px×{v}" for k, v in edges.items())))
            print(f"{label}: cards={res['cards']} reposts={res['reposts']} "
                  f"wrapped={len(res['wrapped'])} unnamed={len(res['unnamed'])} "
                  f"noTime={len(res['noTime'])} overflow={len(res['overflow'])} "
                  f"stubs={len(res['stubs'])} nameLoses={len(res['nameLoses'])} "
                  f"planted={res.get('planted', 0)}")

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
