#!/usr/bin/env python3
"""A real instance, loaded the way a phone loads it: cold, then again, over a throttled 4G link.

The self-contained half (check_client_load_speed.py) proves the shell's scripts can load in parallel.
This half measures what a user actually waits for through the real path (CDN, TLS, nginx, the
server's cache headers), because that is where "stuck loading on mobile" was reported and a loopback
check cannot see a CDN or a header. Measured before the fix on poster.place with this profile:
15.7 s cold and 13.3 s repeat to the first painted feed; the budgets below are what a phone should
never exceed.

    venv-unified/bin/python scripts/check_client_load_speed_live.py https://poster.place

Exit 0 = within budget, 1 = over budget (printed), 2 = could not run.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_client_load_speed as base  # noqa: E402

# 4G-ish: 9 Mbps down, 170 ms round trip. Chrome's "Fast 3G" preset is 1.6 Mbps / 560 ms.
NETWORK = {"offline": False, "latency": 170, "downloadThroughput": 9000 * 1024 / 8,
           "uploadThroughput": 750 * 1024 / 8}
BUDGET = {"cold": 10.0, "repeat": 6.0}   # seconds to DOMContentLoaded (the app can start)


def main():
    url = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
    if not url:
        print("SKIP: needs an instance URL (./test.sh --live URL)")
        return 2
    chrome = base.find_chrome()
    if not chrome:
        print("SKIP: no Chrome/Chromium on PATH")
        return 2
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP: python 'websockets' not installed")
        return 2
    proc, ws_url = base.launch_chrome(chrome)
    if not proc:
        print("SKIP: Chrome did not expose a debugging endpoint")
        return 2
    try:
        results = asyncio.run(base.measure(ws_url, url + "/", network=NETWORK,
                                           runs=("cold", "repeat"), settle=3))
    finally:
        proc.terminate()
    fails = []
    for r in results:
        boot = r["boot"]
        print(f"{r['label']:6} boot={boot and round(boot, 1)}s painted={r['painted'] and round(r['painted'], 1)}s "
              f"scripts={r['scripts']} concurrency={r['concurrency']} (budget {BUDGET[r['label']]}s)")
        if boot is None or boot > BUDGET[r["label"]]:
            fails.append(f"{r['label']} load: {boot and round(boot, 1)}s before the app could start "
                         f"(budget {BUDGET[r['label']]}s on 4G) — a phone sees a blank shell until then")
    for f in fails:
        print("FAIL:", f)
    if not fails:
        print("OK: within the phone budget, cold and repeat")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
