#!/usr/bin/env python3
"""Desktop mode on a real instance, used for a while: opening a window stays fast and stays the same speed.

Reported 2026-10-28 from a tablet: "super laggy opening any desktop apps. social took about 20 s, same for messages".
Nothing measured how long a desktop-mode window takes to open, or whether that grows over a session, so no check
could have said whether it was new. This opens Social, Messages and Notifications over and over at the tablet's page size
(1408x880, 4x CPU), timing each to its first real content and sampling the JS heap and the live element count.

    venv-unified/bin/python scripts/check_desktop_windows_live.py https://poster.place

A guest session: it signs nothing and creates no account. Exit 0 = within budget, 1 = over (printed), 2 = could not run.
"""
import asyncio
import importlib.util
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_client_load_speed as base  # noqa: E402

base.PORT = int(os.environ.get("PC_CHECK_PORT") or 9496)
base.PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-desktop-windows-check"
ROUNDS = 10
OPEN_BUDGET_S = 5.0          # one window, first real content, phone-speed CPU
GROWTH_S = 1.5               # the last rounds may not be slower than the first by more than this
HEAP_GROWTH = 2.5            # nor hold more than this many times the heap
# The FOCUSED window must be the one asked for (its title is the app's sidebar label) and hold real content: routeView
# re-focuses an open window, so "some window has text" would pass on the previous one.
FOCUSED_IS = ("(()=>{const w=document.querySelector('#os-root .osw.focused');if(!w)return false;"
              "const n=document.querySelector('.nav-item[data-view=%s] span');"
              "const t=(w.querySelector('.osw-title')||{}).textContent||'';"
              "return !!n&&t.trim()===n.textContent.trim()&&w.querySelector('.osw-body').innerText.trim().length>20"
              "&&!w.querySelector('.osw-body .spinner')%s})()")
WINDOWS = {
    "social": ("global", FOCUSED_IS % ("global", "&&w.querySelectorAll('.note').length>0")),
    "messages": ("messages", FOCUSED_IS % ("messages", "")),
    "notifications": ("notifications", FOCUSED_IS % ("notifications", "")),
}


async def run(ws_url, url):
    import websockets
    async with websockets.connect(ws_url, max_size=None) as ws:
        seq, pending = 0, {}

        async def reader():
            async for raw in ws:
                r = json.loads(raw)
                if r.get("id") in pending:
                    pending.pop(r["id"]).set_result(r.get("result", {}))
        task = asyncio.ensure_future(reader())

        async def call(method, params=None):
            nonlocal seq
            seq += 1
            fut = asyncio.get_running_loop().create_future()
            pending[seq] = fut
            await ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, 60)

        async def ev(expr):
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return (r.get("result") or {}).get("value")

        async def until(expr, limit):
            t = time.time()
            while time.time() - t < limit:
                try:
                    if await ev(expr):
                        return time.time() - t
                except Exception:
                    pass
                await asyncio.sleep(0.15)
            return None

        await call("Emulation.setDeviceMetricsOverride", {"width": 1408, "height": 880, "deviceScaleFactor": 1.5,
                                                          "mobile": False})
        await call("Emulation.setCPUThrottlingRate", {"rate": 4})
        await call("Performance.enable")
        await call("Page.navigate", {"url": url + "/client"})
        if await until("!!(window.__PC&&window.PCOS)", 90) is None:
            return None, "the client never started"
        await ev("__PC.switchView('global');true")
        await asyncio.sleep(4)
        await ev("PCOS.enter();true")
        if await until("document.body.classList.contains('os-on')", 30) is None:
            return None, "desktop mode never came up"
        rounds = []
        for _ in range(ROUNDS):
            row = {}
            for name, (view, ready) in WINDOWS.items():
                await ev("PCOS.routeView(%s);true" % json.dumps(view))
                took = await until(ready, 30)
                row[name] = None if took is None else round(took, 2)
                await asyncio.sleep(1.5)
            m = {x["name"]: x["value"] for x in (await call("Performance.getMetrics"))["metrics"]}
            row["heapMB"] = round(m["JSHeapUsedSize"] / 1e6, 1)
            row["elements"] = await ev("document.getElementsByTagName('*').length")
            rounds.append(row)
            print(json.dumps(row), flush=True)
        task.cancel()
        return rounds, ""


def judge(rounds):
    fails = []
    for name in WINDOWS:
        times = [r[name] for r in rounds]
        if any(t is None for t in times):
            fails.append(f"{name}: a window never showed its content within 30 s")
            continue
        if max(times) > OPEN_BUDGET_S:
            fails.append(f"{name}: slowest open {max(times)} s (budget {OPEN_BUDGET_S} s at phone speed)")
        early, late = statistics.median(times[:3]), statistics.median(times[-3:])
        if late - early > GROWTH_S:
            fails.append(f"{name}: opening got slower over the session ({early} s -> {late} s)")
    h0, h1 = min(r["heapMB"] for r in rounds[:3]), rounds[-1]["heapMB"]
    if h0 and h1 / h0 > HEAP_GROWTH:
        fails.append(f"JS heap grew {h0} MB -> {h1} MB over {len(rounds)} rounds")
    return fails


def main():
    url = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
    if not url:
        print("SKIP: needs an instance URL (./test.sh --live URL)")
        return 2
    chrome = base.find_chrome()
    if not chrome:
        print("SKIP: no Chrome/Chromium on PATH")
        return 2
    if importlib.util.find_spec("websockets") is None:
        print("SKIP: python 'websockets' not installed")
        return 2
    proc, ws_url = base.launch_chrome(chrome)
    if not proc:
        print("SKIP: Chrome did not expose a debugging endpoint")
        return 2
    try:
        rounds, why = asyncio.run(run(ws_url, url))
    finally:
        proc.terminate()
    if rounds is None:
        print("FAIL:", why)
        return 1
    fails = judge(rounds)
    for f in fails:
        print("FAIL:", f)
    if not fails:
        print(f"OK: {len(rounds)} rounds of Social / Messages / Notifications in desktop mode, every open under "
              f"{OPEN_BUDGET_S} s, no slowdown and no heap growth over the session")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
