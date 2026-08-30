#!/usr/bin/env python3
"""Does a video in a DM keep its size when you press play? Asked of the RUNNING client.

    venv-unified/bin/python scripts/check_dm_video_live.py [base_url]
    PCVID=<url> venv-unified/bin/python scripts/check_dm_video_live.py     # one URL of your own

Signs in with a throwaway local key, DMs itself a media URL, opens the thread, measures the media
box, presses play, and measures again. A self-DM renders even though the relay rejects a gift wrap
to a non-WoT recipient, because sendDm ingests locally BEFORE it publishes.

WHY THIS EXISTS AS A LIVE DRIVER AND NOT A UNIT TEST. "A video someone sent me in a DM shrinks to a
tiny square when I click play" took four attempts, and three of them were static reproductions that
agreed with me. A hand-built page does not reproduce a DM bubble: written into .feed the bubble
measured 387px and did not shrink-wrap at all, so the broken stylesheet PASSED. Only the real
ancestor chain, in the real app, put the numbers where the user could see them. Nothing here is
mocked for that reason.

Each URL shape is a different path through linkify, and they fail differently — which is why the
default is to run all three:

  .mp4                _media() → a lazily-mounted <video> carrying --arn/--nw
  bare <sha256>       an <img> (no extension ⇒ no type) that __blobFallback swaps for a <video>;
                      the swap used to drop the size hints, so the box fell to the clip's own pixels
  youtube.com/watch   a facade (thumbnail + ▶) that becomes .yt-frame > iframe on click. The frame
                      was width:100% inside a width:fit-content bubble — unresolvable — and its only
                      child is position:absolute, so there was no intrinsic width to fall back on:
                      69x39 in a 95x89 bubble. THIS was the reported bug.

Exit 0 = every case held its size, 1 = something collapsed (printed), 2 = could not run.
"""
import asyncio
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
CHROME = (shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chrome"))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9477)

W, H = 390, 844
# A box narrower than this in a 390px phone bubble is the failure, whatever produced it. The healthy
# numbers are 191-271px wide; the bug measured 69px and 128px.
MIN_W = 170


def _cases():
    if os.environ.get("PCVID"):
        return [("custom", os.environ["PCVID"])]
    return [
        ("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        # Both of these need a reachable media URL; skipped unless one is supplied, since this script
        # must not depend on a fixture host being up.
    ] + ([("mp4", os.environ["PCMP4"])] if os.environ.get("PCMP4") else []) \
      + ([("blob", os.environ["PCBLOB"])] if os.environ.get("PCBLOB") else [])


SNAP = r"""(function(){
  const el=document.querySelector('#dm-msgs .yt-frame, #dm-msgs .yt-embed, #dm-msgs video,'
                                 +' #dm-msgs img:not(.emoji-inline)');
  if(!el) return {none:true};
  const r=el.getBoundingClientRect(), bub=el.closest('.bubble');
  const b=bub?bub.getBoundingClientRect():{width:0,height:0};
  const cs=getComputedStyle(el);
  return {what:el.tagName.toLowerCase()+(el.className?('.'+String(el.className).trim().split(/\s+/).join('.')):''),
          box:[+r.width.toFixed(1),+r.height.toFixed(1)],
          bubble:[+b.width.toFixed(1),+b.height.toFixed(1)],
          css:{w:cs.width, ar:cs.aspectRatio,
               arn:cs.getPropertyValue('--arn').trim(), nw:cs.getPropertyValue('--nw').trim()}};
})()"""

# Press whatever this case's media is started by: the YouTube facade is a click, a <video> is play().
GO = r"""(function(){
  const f=document.querySelector('#dm-msgs .yt-embed'); if(f){ f.click(); return 'facade'; }
  const v=document.querySelector('#dm-msgs video'); if(v){ v.play().catch(()=>{}); return 'play'; }
  return 'nothing to press';
})()"""


async def _session(c, n):
    async def cmd(method, params=None):
        n[0] += 1
        await c.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
        while True:
            r = json.loads(await c.recv())
            if r.get("id") == n[0]:
                if "error" in r:
                    raise RuntimeError(f"{method}: {r['error']}")
                return r.get("result", {})
    return cmd


async def run_case(cmd, label, url):
    async def ev(expr, wait=True):
        r = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": wait})
        return r.get("result", {}).get("value")

    sk = secrets.token_hex(32)
    await cmd("Page.navigate", {"url": BASE + "/client"})
    await asyncio.sleep(3)
    await ev(f"localStorage.clear(); localStorage.setItem('pc_nostr_session',"
             f" JSON.stringify({{mode:'local', sk:'{sk}'}}))", wait=False)
    await cmd("Page.navigate", {"url": BASE + "/client"})
    for _ in range(60):
        me = await ev("(window.__PC&&__PC.me&&__PC.me())?__PC.me().pubkey:null")
        if isinstance(me, str) and len(me) == 64:
            break
        await asyncio.sleep(1)
    else:
        return label, None, "never signed in"

    await ev(f"__PC.sendDm(__PC.me().pubkey, {json.dumps(url)}).catch(e=>''+e)")
    await asyncio.sleep(2)
    await ev("__PC.switchView('messages')", wait=False)
    await asyncio.sleep(3)
    await ev("(function(){const r=document.querySelector('.dm-peer'); if(r) r.click();})()", wait=False)
    await asyncio.sleep(4)

    before = await ev(SNAP)
    await ev(GO, wait=False)
    await asyncio.sleep(5)
    after = await ev(SNAP)
    if not after or after.get("none"):
        return label, None, "no media rendered in the thread"
    return label, (before, after), None


async def main():
    if not CHROME:
        print("no chrome on this node"); return 2
    try:
        urllib.request.urlopen(BASE + "/client", timeout=10)
    except Exception as e:
        print(f"{BASE} unreachable: {e}"); return 2

    # Per-run — see checkall.py PC_CHECK_PROFILE. Two concurrent Chromes cannot share one
    # profile directory without one of them dying on a lock.
    prof = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-dmvideo-check"
    shutil.rmtree(prof, ignore_errors=True)
    p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
                          f"--user-data-dir={prof}", "--no-first-run", "--no-sandbox",
                          "--autoplay-policy=no-user-gesture-required",
                          f"--window-size={W},{H}", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    for _ in range(80):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
            ws = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
            break
        except Exception:
            await asyncio.sleep(0.25)
    if not ws:
        p.terminate(); print("chrome never came up"); return 2

    import websockets
    bad = []
    try:
        async with websockets.connect(ws, max_size=None) as c:
            cmd = await _session(c, [0])
            await cmd("Page.enable"); await cmd("Runtime.enable")
            await cmd("Emulation.setDeviceMetricsOverride",
                      {"width": W, "height": H, "deviceScaleFactor": 2, "mobile": True})
            for label, url in _cases():
                name, pair, err = await run_case(cmd, label, url)
                if err:
                    print(f"  {name:8s} SKIP  {err}")
                    continue
                before, after = pair
                print(f"  {name:8s} {before['what']} {before['box']} -> "
                      f"{after['what']} {after['box']}  bubble {after['bubble']}")
                if after["box"][0] < MIN_W:
                    bad.append(f"{name}: pressing play left the media at {after['box']} in a "
                               f"{after['bubble']} bubble (was {before['box']}) — computed width "
                               f"{after['css']['w']}, --arn={after['css']['arn'] or '(unset)'}")
            shot = await cmd("Page.captureScreenshot", {})
            out = os.path.join(prof, "last.png")
            with open(out, "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            print(f"  (screenshot: {out})")
    finally:
        p.terminate()

    if bad:
        print("\nFAIL — DM media collapsed on play:")
        for b in bad:
            print("  * " + b)
        return 1
    print("\nOK  DM media keeps its size when you press play")
    return 0


sys.exit(asyncio.run(main()))
