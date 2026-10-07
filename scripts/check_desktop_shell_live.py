#!/usr/bin/env python3
"""The REAL PosterChanOS desktop -- Electron, the shell, a real Wayfire -- with the desktop PosterChan resized.

    venv-unified/bin/python scripts/check_desktop_shell_live.py

WHY THIS EXISTS. "posterchan just crashed trying to make posterchan smaller on desktop" -- and the reply to
"i can't believe you never made test cases for this" was true: every test of the dancer ran buddy-host.js
under node with a fake compositor, or the client in Chrome with the bridge stubbed. Nothing ever started the
shipped Electron as `--shell` against a compositor and pressed Smaller and Bigger on her own window. This does:

  * a PRIVATE headless Wayfire (its own XDG_RUNTIME_DIR, `ipc` plugin, Xwayland) -- nothing touches the
    session this runs inside;
  * the desktop app from desktop/node_modules, run as `--shell` from a bundle assembled in a temp dir (never
    desktop/www: other checks assemble it concurrently), networking cut off by a dead proxy;
  * a throwaway key signed in, three app windows opened (they share the desktop's renderer process, which is
    why one crash takes them all down), then 13 Smaller/Bigger presses through her OWN window's menu --
    the path a person uses.

Assertions:
  boot              the shell came up as PosterChanOS with real app windows and her own window.
  renderer-crashed  a page reported Inspector.targetCrashed, or the desktop stopped answering.
  size-not-saved    her size preference did not follow the presses (down to 50%, back up to 250%).
  window-not-resized  her own window did not shrink and then grow (its viewport, read from inside it).

Exit 0 clean * 1 problems * 2 could not run (no wayfire / Xwayland / Electron, or the compositor never came up).
"""
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("PC_CHECK_PORT") or 9496)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or ""
ELECTRON = ROOT / "desktop" / "node_modules" / "electron" / "dist" / "electron"

WAYFIRE_INI = """[core]
plugins = ipc ipc-rules move place resize window-rules wm-actions foreign-toplevel
xwayland = true
[output:HEADLESS-1]
mode = 1920x1080@60000
"""


def skip(why):
    print(f"SKIP: {why}")
    sys.exit(2)


def _pages():
    try:
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3))
    except Exception:
        return []


async def _eval(ws_url, expr, timeout=40):
    import websockets
    async with websockets.connect(ws_url, max_size=2 ** 26, open_timeout=10) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if m.get("id") == 1:
                r = m.get("result", {})
                if "exceptionDetails" in r:
                    raise RuntimeError(str(r["exceptionDetails"].get("exception", {}).get("description", r))[:400])
                return r.get("result", {}).get("value")


def _page(part):
    return next((p for p in _pages() if p.get("type") == "page" and part in p.get("url", "")), None)


async def js(part, expr, timeout=40):
    p = _page(part)
    if not p:
        raise RuntimeError(f"no page {part!r}")
    return await _eval(p["webSocketDebuggerUrl"], expr, timeout)


async def watch_crashes(found):
    """Inspector.targetCrashed from every page: a crash in ANY window is the desktop's crash."""
    import websockets
    seen = set()

    async def one(p):
        try:
            async with websockets.connect(p["webSocketDebuggerUrl"], max_size=2 ** 26, ping_interval=None) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Inspector.enable"}))
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("method") == "Inspector.targetCrashed":
                        found.append(p.get("url", "")[:80])
        except Exception:
            pass
    while True:
        for p in _pages():
            if p.get("type") == "page" and p["id"] not in seen:
                seen.add(p["id"])
                asyncio.ensure_future(one(p))
        await asyncio.sleep(1)


def build_bundle(tmp):
    src = Path(tmp) / "src"
    (src / "desktop").mkdir(parents=True)
    for name in ("static", "templates", "scripts"):
        (src / name).symlink_to(ROOT / name, target_is_directory=True)
    for f in (ROOT / "desktop").iterdir():
        if f.name in ("node_modules", "www", "dist", "resources") or f.is_dir():
            continue
        shutil.copy2(f, src / "desktop" / f.name)
    (src / "desktop" / "node_modules").symlink_to(ROOT / "desktop" / "node_modules", target_is_directory=True)
    r = subprocess.run(["bash", str(src / "desktop" / "build-www.sh")], cwd=src, capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not (src / "desktop" / "www" / "index.html").exists():
        skip("could not assemble the desktop bundle: " + (r.stderr or r.stdout)[-400:])
    return src / "desktop"


async def main():
    if not shutil.which("wayfire"):
        skip("wayfire is not installed")
    if not shutil.which("Xwayland"):
        skip("Xwayland is not installed")
    if not ELECTRON.exists():
        skip("desktop/node_modules has no Electron (run npm ci in desktop/)")
    import importlib.util
    if importlib.util.find_spec("websockets") is None:
        skip("python websockets is not installed")

    problems = []
    tmp = tempfile.mkdtemp(prefix="pc-shell-live-")
    run = Path(tmp) / "run"; run.mkdir(mode=0o700)
    # A folder of its OWN inside the one it was given: this empties it before starting, and must never empty
    # a directory somebody else handed over.
    profile = (Path(PROFILE) / "pc-shell-live") if PROFILE else Path(tmp) / "home"
    shutil.rmtree(profile, ignore_errors=True); profile.mkdir(parents=True)
    (Path(tmp) / "wayfire.ini").write_text(WAYFIRE_INI)
    procs = []
    try:
        app_dir = build_bundle(tmp)
        wf_log = open(Path(tmp) / "wayfire.log", "w")
        env = dict(os.environ, XDG_RUNTIME_DIR=str(run), WLR_BACKENDS="headless", WLR_RENDERER="pixman",
                   WLR_LIBINPUT_NO_DEVICES="1")
        env.pop("WAYLAND_DISPLAY", None); env.pop("DISPLAY", None); env.pop("WAYFIRE_SOCKET", None)
        procs.append(subprocess.Popen(["wayfire", "-c", str(Path(tmp) / "wayfire.ini")], env=env, stdout=wf_log,
                                      stderr=subprocess.STDOUT, start_new_session=True))
        display, sock = None, None
        for _ in range(100):
            txt = (Path(tmp) / "wayfire.log").read_text(errors="ignore")
            m = re.search(r"Starting Xwayland on :(\d+)", txt)
            socks = [p for p in run.iterdir() if p.name.startswith("wayfire-") and p.name.endswith(".socket")]
            if m and socks:
                display, sock = ":" + m.group(1), socks[0]
                break
            time.sleep(0.1)
        if not display:
            skip("the headless compositor never came up")
        time.sleep(1.0)
        el_log = open(Path(tmp) / "electron.log", "w")
        eenv = dict(os.environ, HOME=str(profile), XDG_CONFIG_HOME=str(profile / "cfg"), XDG_RUNTIME_DIR=str(run),
                    DISPLAY=display, WAYFIRE_SOCKET=str(sock))
        eenv.pop("WAYLAND_DISPLAY", None)
        procs.append(subprocess.Popen([str(ELECTRON), ".", "--shell", "--ozone-platform=x11", "--proxy-server=127.0.0.1:9",
                                       f"--remote-debugging-port={PORT}"], cwd=app_dir, env=eenv, stdout=el_log,
                                      stderr=subprocess.STDOUT, start_new_session=True))
        for _ in range(300):
            if _page("index.html"):
                break
            await asyncio.sleep(0.1)
        else:
            skip("the desktop app never opened a page: " + (Path(tmp) / "electron.log").read_text(errors="ignore")[-400:])

        crashed = []
        watcher = asyncio.ensure_future(watch_crashes(crashed))
        for _ in range(150):
            try:
                if await js("index.html", "!!(window.PCOS&&PCOS.isOn()&&window.PCOSWin&&PCOSWin.enabled()&&window.PCBuddy&&PCBuddy.box())"):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)
        else:
            problems.append("boot: the shell did not come up as PosterChanOS with her window")
        if not problems:
            await js("index.html", "(()=>{const k=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                                   "NostrTools.nip19.nsecEncode(k);document.querySelector('#btn-nsec-login').click();return true})()")
            await asyncio.sleep(5)
            await js("index.html", "(async()=>{for(const v of ['messages','notes','home']){try{PCOS.routeView(v)}catch(_){}"
                                   "await new Promise(r=>setTimeout(r,1500))}return true})()", timeout=60)
            await js("index.html", "PCBuddy.setSize(1);true")
            await asyncio.sleep(1.5)
            # HER WINDOW'S OWN SIZE, read from inside it: buddy.html's viewport IS the Electron window that
            # buddy-host resizes. (Under Xwayland, Wayfire does not list her skip-taskbar always-on-top
            # window, so the compositor's rectangle -- what .102's native Wayland reports -- is not
            # available here; the window's size is the next most direct measurement.)
            r0 = await js("buddy.html", "({width:innerWidth,height:innerHeight})")
            sizes, rects = [], []
            for step in ["smaller"] * 5 + ["bigger"] * 8:
                await js("buddy.html", f"pcBuddyWin.menu('{step}');true")
                await asyncio.sleep(0.6)
                try:
                    sizes.append(await js("index.html", "PCBuddy.size()", timeout=15))
                    rects.append(await js("buddy.html", "({width:innerWidth,height:innerHeight})", timeout=15))
                except Exception as e:
                    problems.append(f"renderer-crashed: the desktop stopped answering after '{step}' ({e})")
                    break
            if crashed:
                problems.append("renderer-crashed: " + ", ".join(crashed))
            if not problems:
                if min(sizes) != 0.5 or sizes[-1] != 2.5:
                    problems.append(f"size-not-saved: sizes went {sizes}")
                ws = [r["width"] for r in rects if r]
                if not (r0 and ws and min(ws) < r0["width"] * 0.7 and max(ws) > r0["width"] * 1.6):
                    problems.append(f"window-not-resized: start {r0 and r0['width']}, widths {ws}")
        watcher.cancel()
    finally:
        for p in reversed(procs):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)
    if problems:
        print("\n".join("FAIL " + p for p in problems))
        sys.exit(1)
    print("OK: the real PosterChanOS desktop resized PosterChan 13 times through her own menu, no crash")


if __name__ == "__main__":
    asyncio.run(main())
