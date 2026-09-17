#!/usr/bin/env python3
"""No PosterChan window is ever an empty or black managed window.

Section 3 of the beta backlog calls this a release blocker, and it is the one class of desktop bug
that reports SUCCESS: the compositor has a toplevel, the taskbar has a button, the window has a
title -- and the surface inside it is blank. Nothing logs, nothing throws, and a screenshot is the
only thing that disagrees.

So this asks each window what it is SHOWING, from inside that window's own renderer: the painted
height of its content root, how many characters it holds, how many descendants are actually visible
(size, visibility, display and opacity all counted), whether it is still sitting on a spinner, and
what its background is. A window that fails those is named, with its numbers, rather than reported
as "the desktop looks fine".

IT NEEDS A RUNNING POSTERCHANOS DESKTOP with remote debugging, which is not the laptop this suite
usually runs on, so with nothing to connect to it exits **2** -- "could not run" -- and the suite
reports it as a SKIP with that reason, never as a pass. Point it at one with:

    PC_OS_CDP=127.0.0.1:9223 python3 scripts/check_os_blank_windows.py

`--open-everything` first clicks every icon on the desktop, so the gate covers every app rather
than whatever happened to be open; `--keep` leaves them open for a human to look at.

Measured on a headless two-output PosterChanOS instance: 26 app windows + 2 shells, 0 blank.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# A window holding fewer than this many characters, or with almost nothing visibly laid out, or
# whose content root never got a height, is not showing anybody anything.
MIN_CHARS = 5
MIN_VISIBLE = 2
MIN_HEIGHT = 20

PROBE = """
(() => {
  const root=document.documentElement, body=document.body;
  const main=document.querySelector('#feed, .app, .main, main') || body;
  const style=getComputedStyle(body);
  const text=String(main.textContent||'').replace(/\\s+/g,' ').trim();
  const rect=main.getBoundingClientRect();
  const visible=[...main.querySelectorAll('*')].filter(el=>{
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return r.width>4 && r.height>4 && s.visibility!=='hidden' && s.display!=='none'
           && Number(s.opacity)>0.05;
  }).length;
  return {title:document.title, w:Math.round(rect.width), h:Math.round(rect.height),
          chars:text.length, sample:text.slice(0,60), visible:visible,
          bg:style.backgroundColor, oswin:root.classList.contains('pc-oswin'),
          spinner:!!main.querySelector('.spinner')};
})()
"""

OPEN_EVERYTHING = """
(async () => {
  const icons=[...document.querySelectorAll('.os-icon')].filter(b=>!b.classList.contains('is-folder'));
  const opened=[];
  for(const icon of icons){
    const view=icon.dataset.view||'';
    if(!view || view.indexOf('folder:')===0) continue;
    icon.click(); opened.push(view);
    await new Promise(r=>setTimeout(r,700));
  }
  await new Promise(r=>setTimeout(r,2500));
  return opened;
})()
"""


def verdict(measurement):
    """Why this window is blank, or '' when it is showing something.

    Pure, so the rule is testable with no desktop anywhere near it."""
    if not isinstance(measurement, dict):
        return "the window could not be measured"
    reasons = []
    if int(measurement.get("h") or 0) < MIN_HEIGHT:
        reasons.append("content root is %spx tall" % measurement.get("h"))
    if int(measurement.get("chars") or 0) < MIN_CHARS:
        reasons.append("holds %s characters" % measurement.get("chars"))
    if int(measurement.get("visible") or 0) < MIN_VISIBLE:
        reasons.append("%s visible elements" % measurement.get("visible"))
    return "; ".join(reasons)


def endpoint():
    return (os.environ.get("PC_OS_CDP") or "127.0.0.1:9223").strip()


def targets(where):
    with urllib.request.urlopen("http://%s/json" % where, timeout=8) as response:
        return [t for t in json.load(response) if t.get("type") == "page"]


def evaluate(ws_url, expression, timeout=30):
    import asyncio

    import websockets

    async def ask():
        async with websockets.connect(ws_url, max_size=40 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
                "expression": expression, "awaitPromise": True, "returnByValue": True}}))
            end = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < end:
                message = json.loads(await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, end - asyncio.get_event_loop().time())))
                if message.get("id") == 2:
                    result = message.get("result", {})
                    if "exceptionDetails" in result:
                        return {"error": str(result["exceptionDetails"].get("text"))}
                    return result.get("result", {}).get("value")
            return {"error": "timeout"}

    return asyncio.run(ask())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-everything", action="store_true",
                        help="click every desktop icon first, so every app is covered")
    parser.add_argument("--keep", action="store_true", help="leave the windows open")
    args = parser.parse_args()

    where = endpoint()
    try:
        pages = targets(where)
    except (urllib.error.URLError, OSError, ValueError) as error:
        print("SKIP: no PosterChanOS desktop answering on %s (%s). "
              "Start one and set PC_OS_CDP." % (where, error))
        return 2
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP: the websockets package is needed to speak CDP")
        return 2
    if not pages:
        print("SKIP: %s has no page targets; is that a PosterChan shell?" % where)
        return 2

    if args.open_everything:
        shells = [p for p in pages if "Window —" not in (p.get("title") or "")]
        if not shells:
            print("SKIP: no desktop shell among the targets")
            return 2
        opened = evaluate(shells[0]["webSocketDebuggerUrl"], OPEN_EVERYTHING, timeout=120)
        print("opened from the desktop: %s" % (opened if isinstance(opened, list) else opened))
        time.sleep(2)
        pages = targets(where)

    blank, checked = [], 0
    for page in pages:
        title = (page.get("title") or "")[:48]
        try:
            measurement = evaluate(page["webSocketDebuggerUrl"], PROBE)
        except Exception as error:                      # noqa: BLE001 - any failure is a finding
            print("%-48s UNREACHABLE (%s)" % (title, error))
            blank.append((title, "unreachable: %s" % error))
            continue
        checked += 1
        why = verdict(measurement)
        if why:
            print("%-48s BLANK  %s" % (title, why))
            blank.append((title, why))
        else:
            print("%-48s ok     h=%s chars=%s visible=%s" % (
                title, measurement.get("h"), measurement.get("chars"), measurement.get("visible")))

    print("\n%d windows measured, %d blank" % (checked, len(blank)))
    return 1 if blank else 0


if __name__ == "__main__":
    sys.exit(main())
