#!/usr/bin/env python3
"""The desktop behaviours section 3 calls release blockers, measured on a real compositor.

Four things a person does every day, each of which has been reported broken at least once and none
of which any unit test can see, because they live between the compositor, the shell renderer and the
window a person is actually looking at:

  alt-tab     Alt+Tab moves focus to another window (the chord, held, not a single tick).
  clipboard   A copy inside one window reaches the compositor selection AND a real Ctrl+V pastes it
              into a different window.
  snap        Dragging a title bar to a screen edge snaps the window to half that output.
  cross       Dragging it onto the other output leaves it there.

IT NEEDS A RUNNING POSTERCHANOS DESKTOP with remote debugging and a Wayfire IPC socket, which the
machine running this suite usually is not: with nothing to drive it exits **2** ("could not run"),
reported as a SKIP with its reason, never as a pass.

    PC_OS_CDP=127.0.0.1:9223 PC_OS_WF=/run/user/1000/wayfire-wayland-1.socket \\
        python3 scripts/check_os_desktop_behaviour.py

TWO THINGS THIS SCRIPT LEARNED THE HARD WAY, both of which made a working desktop look broken:
  * `pc-wayfire-action` (Alt+Tab's command) finds its socket through XDG_RUNTIME_DIR, NOT
    WAYFIRE_SOCKET — point that at the session under test or the tick goes to a different desktop.
  * Wayfire's stipc takes `{"combo": "BTN_LEFT", "mode": "press"}`. Any other shape answers an error
    nobody reads, so the pointer glides over the title bar without ever pressing and a perfectly
    good snap measures as "free-floating".
"""
import argparse
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request


def endpoint():
    return (os.environ.get("PC_OS_CDP") or "127.0.0.1:9223").strip()


def wayfire_socket():
    return (os.environ.get("PC_OS_WF") or os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid()), "wayfire-wayland-1.socket"))


class Skip(Exception):
    """Something this check needs is not here. Never a failure — a reason."""


# ---------------------------------------------------------------- the compositor
def wf(method, data=None):
    path = wayfire_socket()
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(8)
        s.connect(path)
    except OSError as error:
        raise Skip("no Wayfire IPC socket at %s (%s)" % (path, error))
    payload = json.dumps({"method": method, "data": data or {}}).encode()
    s.send(struct.pack("=i", len(payload)) + payload)
    size = struct.unpack("=i", s.recv(4))[0]
    body = b""
    while len(body) < size:
        body += s.recv(size - len(body))
    s.close()
    return json.loads(body)


def toplevels():
    got = wf("window-rules/list-views")
    rows = got if isinstance(got, list) else got.get("info", got)
    return [r for r in rows if isinstance(r, dict) and r.get("role") == "toplevel"]


def outputs():
    got = wf("window-rules/list-outputs")
    return got if isinstance(got, list) else got.get("info", got)


def activated():
    for view in toplevels():
        if view.get("activated"):
            return view
    return None


def key(name, down):
    wf("stipc/feed_key", {"key": name, "state": bool(down)})


def button(down):
    got = wf("stipc/feed_button", {"combo": "BTN_LEFT", "mode": "press" if down else "release"})
    if not isinstance(got, dict) or got.get("result") != "ok":
        raise Skip("stipc refused a button: %s" % got)


def cursor(x, y):
    wf("stipc/move_cursor", {"x": int(x), "y": int(y)})


def drag(start, end, steps=14):
    cursor(*start); time.sleep(0.25)
    button(True); time.sleep(0.25)
    for i in range(1, steps + 1):
        cursor(start[0] + (end[0] - start[0]) * i / steps,
               start[1] + (end[1] - start[1]) * i / steps)
        time.sleep(0.06)
    time.sleep(0.5)
    button(False); time.sleep(1.4)


# ---------------------------------------------------------------- the renderers
def pages():
    where = endpoint()
    try:
        with urllib.request.urlopen("http://%s/json" % where, timeout=8) as response:
            return [t for t in json.load(response) if t.get("type") == "page"]
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Skip("no desktop answering on %s (%s)" % (where, error))


def evaluate(page, expression, timeout=25):
    import asyncio

    try:
        import websockets
    except ImportError:
        raise Skip("the websockets package is needed to speak CDP")

    async def ask():
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
                "expression": expression, "awaitPromise": True,
                "returnByValue": True, "userGesture": True}}))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if message.get("id") == 2:
                    result = message.get("result", {})
                    if "exceptionDetails" in result:
                        return {"error": str(result["exceptionDetails"].get("text"))[:120]}
                    return result.get("result", {}).get("value")

    return asyncio.run(ask())


def windows():
    return [p for p in pages() if "Window" in (p.get("title") or "")]


# ---------------------------------------------------------------- the verdicts (pure, so testable)
def snapped(geometry, output):
    """half / full / floating, for a window on an output. The height is deliberately loose: the
    taskbar and the frame's own margins take ~100px of a real screen, and demanding the whole
    height reads a correct snap as a miss."""
    if not geometry or not output:
        return "unknown"
    tall = geometry["height"] >= output["height"] * 0.7
    if abs(geometry["width"] - output["width"] / 2) < 60 and tall:
        return "half"
    if abs(geometry["width"] - output["width"]) < 60 and tall:
        return "full"
    return "floating"


def moved_focus(before, after):
    """Alt+Tab has to reach ANOTHER window; landing back on the desktop surface is not switching."""
    if not before or not after:
        return False
    return before.get("id") != after.get("id") and "Desktop" not in (after.get("title") or "")


# ---------------------------------------------------------------- the four behaviours
def check_alt_tab():
    before = activated()
    key("KEY_LEFTALT", True); time.sleep(0.2)
    key("KEY_TAB", True); time.sleep(0.12); key("KEY_TAB", False); time.sleep(1.2)
    key("KEY_LEFTALT", False); time.sleep(1.2)
    after = activated()
    ok = moved_focus(before, after)
    return ok, "%s -> %s" % ((before or {}).get("title", "?"), (after or {}).get("title", "?"))


def check_clipboard(app_windows):
    if len(app_windows) < 2:
        raise Skip("two application windows are needed to move a clipboard between them")
    mark = "pc-clip-%d" % int(time.time())
    source, target = app_windows[0], app_windows[1]
    evaluate(source, """
      (() => { const b=document.createElement('textarea'); b.value=%s;
        b.style.cssText='position:fixed;left:-9999px'; document.body.appendChild(b); b.select();
        const ok=document.execCommand('copy'); b.remove(); return ok; })()""" % json.dumps(mark))
    evaluate(target, """
      (() => { const o=document.getElementById('pcclipprobe'); if(o)o.remove();
        const b=document.createElement('textarea'); b.id='pcclipprobe';
        b.style.cssText='position:fixed;left:8px;top:8px;width:280px;height:50px;z-index:99999';
        document.body.appendChild(b); b.focus(); return document.activeElement===b; })()""")
    name = (target.get("title") or "").split("— ")[-1].strip()
    for view in toplevels():
        if name and name in (view.get("title") or ""):
            wf("window-rules/focus-view", {"id": view["id"]})
            break
    time.sleep(0.8)
    key("KEY_LEFTCTRL", True); time.sleep(0.12)
    key("KEY_V", True); time.sleep(0.12); key("KEY_V", False); time.sleep(0.12)
    key("KEY_LEFTCTRL", False); time.sleep(1.0)
    got = evaluate(target, "(()=>{const b=document.getElementById('pcclipprobe');return b?b.value:'';})()")
    return (mark in str(got)), "%r pasted into %s" % (str(got)[:30], name or "?")


def _screen(view):
    return next(o for o in outputs() if o.get("name") == view.get("output-name"))["geometry"]


def _grab(view):
    """Where to press, in CURSOR coordinates.

    A VIEW'S GEOMETRY IS OUTPUT-LOCAL AND THE CURSOR IS GLOBAL. Measured: a window reported
    x=259 while sitting on an output that starts at x=1280 — press at 259 and the pointer is on the
    OTHER screen, where it grabs nothing and the drag reads as "snapping is broken". The output's
    own origin is the conversion, and it is zero on exactly one screen, which is why this can pass
    for as long as the test window happens to live there."""
    box, screen = view["geometry"], _screen(view)
    return (screen["x"] + box["x"] + box["width"] // 2, screen["y"] + box["y"] + 12)


def check_snap(title):
    view = next((v for v in toplevels() if title in (v.get("title") or "")), None)
    if not view:
        raise Skip("no window titled %r to drag" % title)
    screen = _screen(view)
    drag(_grab(view), (screen["x"] + 2, screen["y"] + screen["height"] // 2))
    after = next((v for v in toplevels() if title in (v.get("title") or "")), None)
    where = snapped(after["geometry"] if after else None, screen)
    return where == "half", "left edge -> %s (%s)" % (where, json.dumps((after or {}).get("geometry")))


def check_cross_monitor(title):
    view = next((v for v in toplevels() if title in (v.get("title") or "")), None)
    if not view:
        raise Skip("no window titled %r to drag" % title)
    others = [o for o in outputs() if o.get("name") != view.get("output-name")]
    if not others:
        raise Skip("this desktop has one output; a cross-monitor drag needs two")
    box = others[0]["geometry"]
    drag(_grab(view), (box["x"] + box["width"] // 2, box["y"] + 12))
    after = next((v for v in toplevels() if title in (v.get("title") or "")), None)
    landed = (after or {}).get("output-name")
    return landed == others[0].get("name"), "%s -> %s" % (view.get("output-name"), landed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", default="terminal", help="title fragment of the window to drag")
    args = parser.parse_args()

    try:
        app_windows = windows()
        if len(app_windows) < 2:
            raise Skip("open at least two PosterChan windows first (%d found)" % len(app_windows))
        checks = [("alt-tab", check_alt_tab),
                  ("clipboard", lambda: check_clipboard(app_windows)),
                  ("snap", lambda: check_snap(args.window)),
                  ("cross-monitor", lambda: check_cross_monitor(args.window))]
        failures = 0
        for name, run in checks:
            ok, detail = run()
            print("%-14s %-4s %s" % (name, "ok" if ok else "FAIL", detail))
            failures += 0 if ok else 1
        print("\n%d of %d behaviours failed" % (failures, len(checks)))
        return 1 if failures else 0
    except Skip as reason:
        print("SKIP: %s" % reason)
        return 2


if __name__ == "__main__":
    sys.exit(main())
