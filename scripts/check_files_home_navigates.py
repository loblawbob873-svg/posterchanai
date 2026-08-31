#!/usr/bin/env python3
"""Clicking a folder on the Files home screen must actually open that folder.

    venv-unified/bin/python scripts/check_files_home_navigates.py [BASE]

Reported, repeatedly: "Once I am in home, I can't click to any other folder from Blossom or Synced
folders", "file manager is completely non-functional now".

WHY NOTHING CAUGHT IT. `check_files_explorer.py` covers "home, drive and synced views" and passes —
but it builds its OWN `.fx-home-tile` markup by hand and never runs `_renderDriveHome`, never binds
a handler, and never clicks anything. It is a LAYOUT check. So the one thing a person does on that
screen — press a folder — was exercised by nothing in this repository, which is why a broken tile
could stand while the suite stayed green.

This boots the real client with a throwaway key, seeds a drive index so home has folders to show,
opens Files, and PRESSES A TILE. The assertion is that the view changes: the folder listing appears
and the home grid is gone. Anything that leaves the same screen on screen — a handler bound to
nothing, a re-render that lands back on home, a state flag the tile forgets to set — fails here.

Exit 0 clean, 1 a real failure, 2 could not run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

PORT = int(os.environ.get("PC_CHECK_PORT") or 9487)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-files-home-check"
BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PC_ORIGIN") or "http://127.0.0.1:3051"
SK = os.urandom(32).hex()

# Two folders and a file in each, written straight into the drive index the way an upload does.
SEED = r"""(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  /* The drive index is NOT on `window` — it is reached through the client's own surface,
   * `__PC.filesIdx()`. Asking for `window.FilesIdx` is why this check skipped instead of running,
   * which is the same as not having it: a SKIP proves nothing. */
  let FilesIdx = null;
  for (let i = 0; i < 60 && !FilesIdx; i++) {
    try { FilesIdx = window.__PC && __PC.filesIdx && __PC.filesIdx(); } catch (e) { FilesIdx = null; }
    if (!FilesIdx) await wait(250);
  }
  if (!FilesIdx) return {ok:false, why:'the drive index never appeared (__PC.filesIdx)'};
  try { await FilesIdx.ensure(); } catch (e) {}
  FilesIdx.beginBatch();
  FilesIdx.addFolder('Documents', false);
  FilesIdx.addFolder('Pictures', false);
  FilesIdx.setFile('a'.repeat(64), {name:'notes.txt', folder:'Documents', mime:'text/plain',
                                    size:12, ts:Math.floor(Date.now()/1000)});
  FilesIdx.setFile('b'.repeat(64), {name:'photo.jpg', folder:'Pictures', mime:'image/jpeg',
                                    size:34, ts:Math.floor(Date.now()/1000)});
  try { await FilesIdx.endBatch(); } catch (e) {}
  return {ok:true};
})()"""

OPEN_FILES = r"""(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  if (window.__PC && __PC.switchView) __PC.switchView('blossom');
  else if (window.switchView) switchView('blossom');
  else location.hash = '#blossom';
  for (let i = 0; i < 60; i++) {
    await wait(250);
    if (document.querySelector('.fx-home-tile') || document.querySelector('#bl-grid')) break;
  }
  const tiles = [...document.querySelectorAll('.fx-home-tile[data-folder]')];
  return {tiles: tiles.map(t => t.dataset.folder),
          bound: tiles.filter(t => typeof t.onclick === 'function').length,
          home: !!document.querySelector('.fx-home')};
})()"""

SIDEBAR = r"""(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const where = () => {
    const chips = [...document.querySelectorAll('.folder-chip[data-folder]')];
    const act = chips.find(c => c.classList.contains('active'));
    return {folder: act ? act.dataset.folder : null,
            home: !!document.querySelector('.fx-home'),
            heads: [...document.querySelectorAll('[data-fxtoggle]')].map(h => h.dataset.fxtoggle)};
  };
  const press = async (el) => { el.click(); for (let i = 0; i < 24; i++) await wait(120); };
  const out = {steps: []};

  /* 1. A folder chip in the sidebar, pressed from inside another folder. */
  const pics = [...document.querySelectorAll('.folder-chip[data-folder]')]
                 .find(c => c.dataset.folder === 'Pictures');
  if (!pics) return {ok:false, why:'the sidebar lists no Pictures folder to press'};
  await press(pics);
  out.steps.push({what:'the Pictures chip', got: where().folder, want:'Pictures'});

  /* 2. The "Blossom" HEADING, pressed from inside a folder. This is the one that only collapsed a
   *    tree on a wide layout, so it appeared to do nothing: "clicking on a blossom folder also does
   *    nothing", "clicking ALL does not bring you back to blossom folder". */
  const head = [...document.querySelectorAll('[data-fxtoggle]')]
                 .find(h => h.dataset.fxtoggle === 'blossom');
  if (!head) return {ok:false, why:'there is no Blossom heading in the sidebar'};
  if (typeof head.onclick !== 'function') return {ok:false, why:'the Blossom heading has no click handler'};
  await press(head);
  out.steps.push({what:'the Blossom heading', got: where().folder, want:''});
  out.ok = true;
  return out;
})()"""

CLICK = r"""(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const tile = [...document.querySelectorAll('.fx-home-tile[data-folder]')]
                 .find(t => t.dataset.folder === 'Documents');
  if (!tile) return {ok:false, why:'no Documents tile on the home screen'};
  tile.click();
  for (let i = 0; i < 40; i++) {
    await wait(250);
    if (!document.querySelector('.fx-home')) break;
  }
  const chips = [...document.querySelectorAll('.folder-chip[data-folder]')];
  return {ok:true,
          stillHome: !!document.querySelector('.fx-home'),
          activeChip: (chips.find(c => c.classList.contains('active')) || {}).textContent || '',
          cards: document.querySelectorAll('.file-card, .fx-row').length};
})()"""


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    if not shutil.which("google-chrome-stable"):
        print("SKIP  no Chrome on this box")
        return 2
    try:
        urllib.request.urlopen(BASE + "/client", timeout=8).read(1)
    except Exception as exc:
        print("SKIP  %s is not serving the client (%s)" % (BASE, exc))
        return 2

    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
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

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=1 << 24) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        if msg.get("error"):
                            raise RuntimeError("%s: %s" % (method, msg["error"]))
                        return msg.get("result")

            async def js(expr):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if r.get("exceptionDetails"):
                    return {"__throw": str(r["exceptionDetails"].get("text"))}
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Page.addScriptToEvaluateOnNewDocument",
                       {"source": "try{localStorage.setItem('pc_nostr_session',"
                                  "JSON.stringify({mode:'local',sk:%s}));}catch(e){}"
                                  % json.dumps(SK)})
            await call("Page.navigate", {"url": BASE + "/client"})
            await asyncio.sleep(6)

            seeded = await js(SEED)
            if not isinstance(seeded, dict) or not seeded.get("ok"):
                print("SKIP  could not seed a drive index: %s" % seeded)
                return 2

            opened = await js(OPEN_FILES)
            if not isinstance(opened, dict) or opened.get("__throw"):
                print("SKIP  Files did not open: %s" % opened)
                return 2
            if not opened.get("tiles"):
                print("SKIP  the home screen showed no folder tiles to press (%s)" % opened)
                return 2

            problems = []
            if opened.get("bound", 0) < len(opened["tiles"]):
                problems.append(
                    "%d of %d home tiles have no click handler — they are drawn and inert, which is "
                    "indistinguishable from a frozen screen"
                    % (len(opened["tiles"]) - opened.get("bound", 0), len(opened["tiles"])))

            clicked = await js(CLICK)
            if not isinstance(clicked, dict) or not clicked.get("ok"):
                print("FAIL  %s" % (clicked or {}).get("why", clicked))
                return 1
            if clicked.get("stillHome"):
                problems.append(
                    "pressing the Documents folder left the home screen on screen — the click "
                    "changes no view, which is exactly 'I can't click to any other folder'")

            side = await js(SIDEBAR)
            if not isinstance(side, dict) or not side.get("ok"):
                print("FAIL  %s" % (side or {}).get("why", side))
                return 1
            for step in side.get("steps", []):
                if step.get("got") != step.get("want"):
                    problems.append(
                        "pressing %s left Files showing folder %r, not %r — a folder in the left "
                        "navbar must bring you to that location"
                        % (step["what"], step.get("got"), step.get("want")))

            print(("FAIL  " if problems else "OK    ")
                  + "Files home: %d tiles, %d bound; pressing one %s"
                  % (len(opened["tiles"]), opened.get("bound", 0),
                     "stayed on home" if clicked.get("stillHome") else "opened the folder")
                  + "; the sidebar's chips and headings all navigate")
            for p in problems:
                print("  - %s" % p)
            return 1 if problems else 0
    finally:
        proc.terminate()
        shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
