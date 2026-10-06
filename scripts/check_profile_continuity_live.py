#!/usr/bin/env python3
"""A real profile on a real instance never skips a reply the relay has.

Reported as "there is no way I have an 11 day reply gap" and "huge gap between posts": the relay held
1,502 replies, 20-100 on every day, while the profile jumped eleven days. The cause was client paging
(tests/client/test_profile_has_no_gap_full_app.py covers it against a stub); this is the same question
asked of production: open the account's profile in the shipped client, scroll the Replies tab back, and
for every pair of neighbouring replies on screen ask the instance's relay whether any reply of that
account falls BETWEEN them. One skipped reply is a failure, named by its dates.

    venv-unified/bin/python scripts/check_profile_continuity_live.py https://poster.place [name|npub]

The account defaults to `verita84` (resolved through the instance's NIP-05). Exit 0 = continuous,
1 = the profile skipped replies (printed), 2 = could not run (no Chrome, no relay, the page needs a login).
"""
import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_client_load_speed as base  # noqa: E402

SCROLLS = 6


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pc-check"}), timeout=15) as r:
        return json.loads(r.read().decode())


def _day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


async def _relay_between(relay, pk, since, until):
    import websockets
    out = []
    async with websockets.connect(relay, open_timeout=10, max_size=None) as ws:
        cursor = until
        while True:
            await ws.send(json.dumps(["REQ", "c", {"authors": [pk], "kinds": [1, 1111], "since": since,
                                                    "until": cursor, "limit": 500}]))
            got = []
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if m[0] == "EVENT":
                    got.append(m[2])
                elif m[0] in ("EOSE", "CLOSED"):
                    break
            out += got
            if len(got) < 500:
                break
            cursor = min(e["created_at"] for e in got) - 1
    return [e for e in out if any(t and t[0] == "e" for t in e.get("tags", []))]


async def _profile_replies(ws_url, page):
    import websockets
    async with websockets.connect(ws_url, max_size=None) as ws:
        n = 0

        async def call(method, params=None):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params or {}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == n:
                    return m.get("result", {})

        async def js(expr):
            r = await call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
            return (r.get("result") or {}).get("value")

        await call("Page.enable")
        await call("Page.navigate", {"url": page})
        for _ in range(60):
            if await js("!!document.querySelector('.prof-tab[data-tab=\"replies\"]')"):
                break
            if await js("!!document.querySelector('#auth-gate:not(.hidden)') && !document.querySelector('.prof-tab')"):
                return None
            await asyncio.sleep(.5)
        else:
            return None
        await asyncio.sleep(2)
        await js("document.querySelector('.prof-tab[data-tab=\"replies\"]').click()")
        await asyncio.sleep(2)
        for _ in range(SCROLLS):
            await js("(()=>{const f=document.getElementById('feed');f.scrollTop=f.scrollHeight;f.dispatchEvent(new Event('scroll'));})()")
            await asyncio.sleep(2.5)
        return await js("""[...document.querySelectorAll('#prof-list .note, #prof-list article')]
            .map(n=>{const e=window.Store&&Store.get(n.dataset.id);return e?{id:e.id,t:e.created_at}:null}).filter(Boolean)""")


def main():
    url = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
    who = sys.argv[2] if len(sys.argv) > 2 else "verita84"
    if not url:
        print("SKIP: needs an instance URL (./test.sh --live URL)")
        return 2
    chrome = base.find_chrome()
    if not chrome:
        print("SKIP: no Chrome/Chromium on PATH")
        return 2
    try:
        from app.services.nostr.nostr_service import to_pubkey_hex, npub_of
        pk = to_pubkey_hex(who) if who.startswith("npub") else _get(f"{url}/.well-known/nostr.json?name={who}")["names"][who]
        cfg = _get(f"{url}/client/config")
        relay = cfg.get("relay_url") or cfg.get("relay") or ("wss://" + url.split("://", 1)[1])
    except Exception as e:
        print(f"SKIP: could not resolve the account or the relay ({type(e).__name__}: {e})")
        return 2
    proc, ws_url = base.launch_chrome(chrome)
    if not proc:
        print("SKIP: Chrome did not expose a debugging endpoint")
        return 2
    try:
        shown = asyncio.run(_profile_replies(ws_url, f"{url}/{npub_of(pk)}"))
    finally:
        proc.terminate()
    if shown is None:
        print("SKIP: the profile did not open (the page may need a login)")
        return 2
    if len(shown) < 10:
        print(f"SKIP: only {len(shown)} replies on screen — not enough to judge continuity")
        return 2
    ts = sorted({s["t"] for s in shown}, reverse=True)
    shown_ids = {s["id"] for s in shown}
    try:
        on_relay = asyncio.run(_relay_between(relay, pk, ts[-1], ts[0]))
    except Exception as e:
        print(f"SKIP: could not read the relay {relay} ({type(e).__name__})")
        return 2
    skipped = [e for e in on_relay if e["id"] not in shown_ids and ts[-1] < e["created_at"] < ts[0]]
    print(f"{who}: {len(shown)} replies on the profile, {_day(ts[-1])} .. {_day(ts[0])}; "
          f"{len(on_relay)} on {relay} in that span; {len(skipped)} skipped")
    if skipped:
        days = sorted({_day(e["created_at"])[:10] for e in skipped})
        print(f"FAIL: the profile skipped {len(skipped)} replies the relay has, on {', '.join(days[:12])}"
              + (" …" if len(days) > 12 else ""))
        return 1
    print("OK  the profile shows every reply the relay has in the span it covers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
