#!/usr/bin/env python3
"""Reproduce the DM video bug in the REAL client: sign in with a throwaway local key, send
yourself a DM carrying a video URL, open the thread, then measure the box before and after play."""
import asyncio, json, os, secrets, shutil, subprocess, sys, urllib.request

CHROME = shutil.which("google-chrome-stable")
PORT = 9477
BASE = "http://127.0.0.1:3051"
VIDEO = os.environ.get("PCVID", "http://127.0.0.1:8765/port.mp4")
W, H = int(os.environ.get("PCW", 390)), int(os.environ.get("PCH", 844))
SK = secrets.token_hex(32)
SHOT = os.path.dirname(os.path.abspath(__file__))


async def main():
    prof = "/tmp/pc-live-dm"
    shutil.rmtree(prof, ignore_errors=True)
    p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
                          f"--user-data-dir={prof}", "--no-first-run", "--no-sandbox",
                          "--autoplay-policy=no-user-gesture-required",
                          "--disable-features=IsolateOrigins,site-per-process",
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
        print("no chrome"); return 2

    import websockets
    async with websockets.connect(ws, max_size=None) as c:
        n = [0]
        logs = []

        async def cmd(method, params=None):
            n[0] += 1
            await c.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                r = json.loads(await c.recv())
                if r.get("method") == "Runtime.consoleAPICalled":
                    try:
                        logs.append(" ".join(str(a.get("value", a.get("description", "")))
                                             for a in r["params"]["args"])[:300])
                    except Exception:
                        pass
                if r.get("id") == n[0]:
                    if "error" in r:
                        raise RuntimeError(f"{method}: {r['error']}")
                    return r.get("result", {})

        async def ev(expr, wait=True):
            r = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                               "awaitPromise": wait})
            if r.get("exceptionDetails"):
                return {"__throw": str(r["exceptionDetails"].get("exception", {}).get("description", ""))[:400]}
            return r.get("result", {}).get("value")

        await cmd("Page.enable"); await cmd("Runtime.enable")
        await cmd("Emulation.setDeviceMetricsOverride",
                  {"width": W, "height": H, "deviceScaleFactor": 2, "mobile": W < 820})
        await cmd("Page.navigate", {"url": BASE + "/client"})
        await asyncio.sleep(3)
        await ev(f"localStorage.setItem('pc_nostr_session', JSON.stringify({{mode:'local', sk:'{SK}'}}))", wait=False)
        await cmd("Page.navigate", {"url": BASE + "/client"})

        for _ in range(60):
            me = await ev("(window.__PC && __PC.me && __PC.me()) ? __PC.me().pubkey : null")
            if isinstance(me, str) and len(me) == 64:
                break
            await asyncio.sleep(1)
        else:
            print("never signed in; console:", logs[-8:]); return 1
        print("signed in as", me[:12])

        r = await ev(f"__PC.sendDm(__PC.me().pubkey, {json.dumps(VIDEO)}).then(()=>'sent').catch(e=>'ERR '+e)")
        print("sendDm ->", r)
        await asyncio.sleep(2)
        await ev("__PC.switchView('messages')", wait=False)
        await asyncio.sleep(3)
        # open the only conversation row
        print("peers:", await ev("[...document.querySelectorAll('.dm-peer')].map(e=>e.dataset.peer&&e.dataset.peer.slice(0,10))"))
        await ev("(function(){const r=document.querySelector('.dm-peer'); if(r) r.click(); return !!r})()", wait=False)
        await asyncio.sleep(4)

        SNAP = """(function(){
          const v=document.querySelector('#dm-msgs video');
          if(!v) return {none:true, html:(document.querySelector('#dm-msgs')||{}).innerHTML||'(no pane)'};
          const r=v.getBoundingClientRect(); const b=v.closest('.bubble').getBoundingClientRect();
          const cs=getComputedStyle(v);
          return {vid:[+r.width.toFixed(1),+r.height.toFixed(1)], bub:[+b.width.toFixed(1),+b.height.toFixed(1)],
                  nat:v.videoWidth+'x'+v.videoHeight, rs:v.readyState, paused:v.paused,
                  src:(v.getAttribute('src')||'(none)').slice(0,60), vsrc:(v.dataset.vsrc||'').slice(0,60),
                  mounted:v.dataset.vmount||'no', dim:v.dataset.dim||'-',
                  css:{w:cs.width,h:cs.height,mw:cs.maxWidth,mh:cs.maxHeight,ar:cs.aspectRatio,
                       arn:cs.getPropertyValue('--arn').trim(),nw:cs.getPropertyValue('--nw').trim()}};
        })()"""
        print("BEFORE:", json.dumps(await ev(SNAP), indent=1)[:1400])
        await ev("(function(){const v=document.querySelector('#dm-msgs video'); if(v) v.play().catch(e=>0); return 1})()", wait=False)
        for wait_s in (1, 3, 6):
            await asyncio.sleep(wait_s if wait_s == 1 else wait_s - 1)
            print(f"AFTER +{wait_s}s:", json.dumps(await ev(SNAP))[:900])
        shot = await cmd("Page.captureScreenshot", {})
        import base64
        open(os.path.join(SHOT, "live.png"), "wb").write(base64.b64decode(shot["data"]))
        if logs:
            print("console tail:", logs[-6:])
    p.terminate()
    return 0

sys.exit(asyncio.run(main()))
