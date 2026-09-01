#!/usr/bin/env python3
"""Ask a running PosterChanOS shell what state it is in, over CDP.

WHY THIS EXISTS. The shell wedges occasionally — "my right monitor is still useless, nothing works,
like frozen" — while the page is still PAINTING (a screenshot taken during one showed live widgets
and a running clock). So it is an input/focus fault, not a crash, and every theory about it has to
be checked against the renderer at the moment it happens. Two were checked from the outside and
both were wrong: the modal backdrop does not cover the taskbar (`.os-root` is z-index 300, above
every modal) and the renderers were not spinning (32%/27% after a clean restart, against 39%/21%
while wedged).

The shell's own source already records the mechanism that fits: `pcWM.focus(id)` hands compositor
focus to a native app and takes it from us — "measured on the real machine, `document.hasFocus()`
goes true → false and a `blur` event arrives 1ms later" — and modal()'s comment describes a stale
browser→renderer focus handshake leaving the next thing you open unable to take a keystroke.

THE SMOKING GUN TO LOOK FOR is a page reporting `focus: false` while ITS output is the focused one.
Verified healthy on a working desktop: focusing each output in turn moved `hasFocus` to that page
and away from the other, every time.

    # on the desktop, arm it (it is off by default, and should stay off):
    PC_SHELL_EXTRA_ARGS=--remote-debugging-port=9222 pc-shell-start
    # from anywhere with a route to it:
    ssh -N -L 19222:127.0.0.1:9222 <desktop>
    python3 scripts/probe_shell.py --endpoint http://127.0.0.1:19222

The port is an unauthenticated debugger over a session holding the user's keys, so there is no
default endpoint here and nothing starts it for you.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request

#: One expression, so the whole answer is a single round trip per page.
PROBE = """JSON.stringify({
  screenX: window.screenX, screenY: window.screenY,
  focus: document.hasFocus(),
  active: (document.activeElement || {}).tagName || null,
  visibility: document.visibilityState,
  windows: document.querySelectorAll('.osw').length,
  stashed: document.querySelectorAll('.osw.native-stashed').length,
  modals: document.querySelectorAll('#modal-root > *').length,
  sticky: document.querySelectorAll('.modal-sticky').length,
  backdrops: document.querySelectorAll('.pop-backdrop').length,
  confirms: document.querySelectorAll('.uiconfirm-bg').length,
  menus: document.querySelectorAll('.menu-pop, .os-pop').length,
  modalOpen: document.body.classList.contains('modal-open'),
  animOff: document.body.classList.contains('anim-off'),
  ver: String(window.__VER || '').slice(0, 12)})"""


def pages(endpoint: str):
    tabs = json.load(urllib.request.urlopen(endpoint.rstrip("/") + "/json/list", timeout=5))
    return [t for t in tabs if t.get("type") == "page"]


async def evaluate(ws_url: str, expression: str):
    import websockets  # imported late: the script should explain itself without it installed
    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expression, "returnByValue": True}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                result = msg.get("result", {}).get("result", {})
                if "value" not in result:
                    raise SystemExit(f"evaluate failed: {msg}")
                return json.loads(result["value"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", required=True,
                    help="CDP base URL, e.g. http://127.0.0.1:19222 (usually an ssh -L tunnel)")
    ap.add_argument("--expr", help="evaluate this instead of the standard probe")
    args = ap.parse_args()

    try:
        found = pages(args.endpoint)
    except Exception as exc:                                    # noqa: BLE001 - reported, not raised
        print(f"could not reach {args.endpoint}: {exc}", file=sys.stderr)
        print("is the shell running with PC_SHELL_EXTRA_ARGS=--remote-debugging-port=9222 ?",
              file=sys.stderr)
        return 2                                                # 2 == could not run, never a pass

    if not found:
        print("no pages attached — the shell is not running, or not with the debug port",
              file=sys.stderr)
        return 2

    print(f"{len(found)} shell page(s) at {args.endpoint}")
    for i, page in enumerate(found):
        state = asyncio.run(evaluate(page["webSocketDebuggerUrl"], args.expr or PROBE))
        if args.expr:
            print(f"  page{i}: {state}")
            continue
        flag = ""
        # The thing this script was written to catch. A page that does not hold focus is normal
        # for the output you are not looking at, so this is a hint, never a verdict — pair it with
        # `swaymsg focus output <name>` and re-run.
        if not state.get("focus"):
            flag = "   <- no focus: if this IS the output you are clicking, that is the fault"
        print(f"  page{i} at ({state['screenX']},{state['screenY']}) ver={state['ver']}{flag}")
        for key in ("focus", "active", "visibility", "windows", "stashed", "modals", "sticky",
                    "backdrops", "confirms", "menus", "modalOpen", "animOff"):
            print(f"      {key:11s} {state.get(key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
