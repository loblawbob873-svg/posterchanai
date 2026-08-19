#!/usr/bin/env python3
"""The Folder Sync card, RENDERED — its controls, their order, their icons, and its menu.

This card is nearly all of what a person touches in folder sync, and every previous check drove the
ENGINE through it rather than looking at it. That is how it accumulated eleven equal-looking buttons
of nine different widths, two controls named for one job, and — the one that would have been
invisible until somebody pressed it — four handlers bound as
`card.querySelector('.sync-X').onclick = …` with no null check, which throw the moment a button
moves behind a menu and take every control BELOW them with them, Stop syncing included, while the
card still draws perfectly.

So: a real page, a stubbed filesystem, the real card. What is asserted is what a person sees — the
order of the row, that every label carries a sprite icon and no emoji, that the buttons are one
size, that the menu opens and offers what it should, and that pressing a menu row reaches a real
function rather than throwing.

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9557)
PROF = (os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-synccard")

# Enough of a bridge that the card draws every control, including the two conditional ones.
STUB = r"""
(() => {
  window.pcFs = {
    chunkBytes: 4*1024*1024,
    list: async () => [{ id:'vdisk', dir:'/vdisk' }],
    scanPage: async () => ({ files:{}, skipped:[], total:0, done:true }),
    listTrash: async () => [{ at:'.pc-trash/x/a.txt', to:'a.txt' }],
    trashStat: async () => ({ files:1, bytes:1 }),
    emptyTrash: async () => 0,
    confirmGone: async () => ({ gone:true, parentAlive:true }),
    move: async () => {}, trash: async () => '.pc-trash/x/a.txt',
    read: async () => new Uint8Array(0), hashFile: async () => '',
    watch: async () => {}, unwatch: async () => {},
    power: async () => ({ onBattery:false, charging:true }),
  };
})()"""

LOGIN = r"""(async (nsec) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  try{ sessionStorage.clear(); }catch(_){}
  document.body.classList.remove('guest');
  const g=$('#auth-gate'); if(g) g.classList.remove('hidden');
  const l=$('#auth-login'); if(l) l.classList.remove('hidden');
  const nb=$('#btn-nsec'); if(nb) nb.click(); await sleep(80);
  const inp=$('#nsec-input'); if(!inp) return false;
  inp.value = nsec;
  const go=$('#btn-nsec-login'); if(!go) return false;
  go.click();
  for(let i=0;i<40;i++){ await sleep(250); if(window.__PC && __PC.me && __PC.me()) return true; }
  return false;
})"""

# A folder plus a report carrying a refused restore, so the conditional rescue draws too.
OPEN = r"""(async () => {
  const me = window.__PC.me().pubkey;
  localStorage.setItem('pc_sync_folders_' + me, JSON.stringify([
    { id:'vdisk', key:'CardPair', dir:'/vdisk', name:'CardPair',
      excludes:[], prefs:{}, lastSyncAt:0, lastFullScanAt:0 }]));
  window.__PC.switchView('sync');
  await new Promise(r=>setTimeout(r,400));
  // A refusal on the card is what makes "Put N back everywhere" appear at all.
  window.PCSync._testStatus
    ? window.PCSync._testStatus('vdisk')
    : (function(){
        const rep = { plan:{ upload:[{path:'x', resurrect:true}] }, refusedResurrect:{ n:1 } };
        try{ window.PCSync.setStatus && window.PCSync.setStatus('vdisk','test',rep); }catch(_){}
      })();
  await new Promise(r=>setTimeout(r,400));
  return !!document.querySelector('.sync-card');
})"""

INSPECT = r"""(() => {
  const card = document.querySelector('.sync-card');
  if(!card) return { err:'no card' };
  const row = card.querySelector('.sync-actions');
  if(!row) return { err:'no action row' };
  const btns = [...row.querySelectorAll('button')];
  const EMOJI = /[‼-㊙\u{1F000}-\u{1FAFF}☀-➿⬀-⯿]/u;
  return {
    order: btns.map(b => (b.textContent||'').trim()),
    classes: btns.map(b => b.className),
    icons: btns.map(b => !!b.querySelector('svg use')),
    emoji: btns.filter(b => EMOJI.test(b.textContent||'')).map(b => (b.textContent||'').trim()),
    widths: btns.map(b => Math.round(b.getBoundingClientRect().width)),
    heights: btns.map(b => Math.round(b.getBoundingClientRect().height)),
    display: getComputedStyle(row).display,
  };
})"""

MENU = r"""(async () => {
  const more = document.querySelector('.sync-card .sync-more');
  if(!more) return { err:'no More button' };
  more.click();
  await new Promise(r=>setTimeout(r,250));
  const pop = document.querySelector('.menu-pop');
  if(!pop) return { err:'the menu did not open' };
  const rows = [...pop.querySelectorAll('[data-m]')];
  const EMOJI = /[‼-㊙\u{1F000}-\u{1FAFF}☀-➿⬀-⯿]/u;
  const out = { actions: rows.map(b=>b.dataset.m), labels: rows.map(b=>(b.textContent||'').trim()),
                emoji: rows.filter(b=>EMOJI.test(b.textContent||'')).map(b=>b.textContent.trim()) };
  // Pressing a row must reach a real function. Preview is the one that changes nothing.
  let threw = null;
  window.onerror = (m) => { threw = String(m); };
  const prev = rows.find(b => b.dataset.m === 'preview');
  if(prev){ prev.click(); await new Promise(r=>setTimeout(r,600)); }
  out.threw = threw;
  out.stillThere = !!document.querySelector('.sync-card .sync-forget');
  return out;
})"""


class Tab:
    def __init__(self, port, profile):
        self.port, self.profile, self.proc, self.ws, self.n = port, profile, None, None, 0

    async def start(self, chrome, url):
        import websockets
        subprocess.run(["rm", "-rf", self.profile], check=False)
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
             "--window-size=1280,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            return False
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024)
        await self.call("Runtime.enable")
        await self.call("Page.enable")
        await self.call("Page.addScriptToEvaluateOnNewDocument", {"source": STUB})
        await self.call("Page.navigate", {"url": url})
        for _ in range(80):
            await asyncio.sleep(0.25)
            if await self.js("!!(window.__PC && window.PCSync && window.pcFs)"):
                return True
        return False

    async def call(self, method, params=None):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            r = json.loads(await self.ws.recv())
            if r.get("id") == self.n:
                return r.get("result")

    async def js(self, expr, aw=False):
        r = await self.call("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True, "awaitPromise": aw,
                             "timeout": 60000})
        if r.get("exceptionDetails"):
            if os.environ.get("PC_DEBUG"):
                print("  DEBUG:", json.dumps(r["exceptionDetails"])[:800])
            return None
        return r["result"].get("value")

    def stop(self):
        try:
            self.proc and self.proc.terminate()
        except Exception:
            pass
        subprocess.run(["rm", "-rf", self.profile], check=False)


async def drive(url):
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.services.nostr import bech32 as _b32
    nsec = _b32.encode("nsec", bytes.fromhex(secrets.token_hex(32)))
    t = Tab(PORT, PROF)
    problems = []
    try:
        if not await t.start(chrome, url):
            print("SKIP  the client never finished loading")
            return 2
        if not await t.js(f"({LOGIN})({json.dumps(nsec)})", aw=True):
            print("SKIP  login failed")
            return 2
        if not await t.js(f"({OPEN})()", aw=True):
            print("SKIP  the sync card never drew")
            return 2

        v = await t.js(f"({INSPECT})()") or {}
        if v.get("err"):
            print("SKIP  " + v["err"])
            return 2
        print("  buttons:", json.dumps(v["order"]))

        # 1. the order asked for: the primary first, the destructive one, then the menu last
        names = [x for x in v["order"]]
        def idx(sub):
            for i, n in enumerate(names):
                if sub.lower() in n.lower():
                    return i
            return -1
        i_sync, i_stop, i_more = idx("Sync now"), idx("Stop syncing"), idx("More")
        if -1 in (i_sync, i_stop, i_more):
            problems.append(f"a required control is missing from the row: {names}")
        elif not (i_sync < i_stop < i_more):
            problems.append(f"order is {names}, wanted Sync now … Stop syncing … More")
        else:
            print("  order: Sync now … Stop syncing … More")

        # 2. flat icons, no emoji
        if v["emoji"]:
            problems.append(f"emoji in the action row: {v['emoji']}")
        missing = [n for n, has in zip(names, v["icons"]) if not has]
        if missing:
            problems.append(f"buttons with no sprite icon: {missing}")
        if not v["emoji"] and not missing:
            print(f"  every button carries a sprite icon and no emoji ({len(names)} buttons)")

        # 3. one size
        if v["display"] != "grid":
            problems.append(f"the action row is display:{v['display']} — buttons size to their labels")
        w = v["widths"]
        if w and (max(w) - min(w)) > 2:
            problems.append(f"button widths differ: {w}")
        h = v["heights"]
        if h and (max(h) - min(h)) > 2:
            problems.append(f"button heights differ: {h}")
        if w and (max(w) - min(w)) <= 2:
            print(f"  all {len(w)} buttons are one size ({w[0]}x{h[0]})")

        # 4. the menu opens, offers the rest, and a row reaches a real function
        m = await t.js(f"({MENU})()", aw=True) or {}
        if m.get("err"):
            problems.append(m["err"])
        else:
            print("  menu:", json.dumps(m["labels"]))
            if m["emoji"]:
                problems.append(f"emoji in the menu: {m['emoji']}")
            want = {"preview", "check", "tidy", "trash"}
            if not want.issubset(set(m["actions"])):
                problems.append(f"menu is missing {sorted(want - set(m['actions']))}")
            if m.get("threw"):
                problems.append(f"pressing a menu row threw: {m['threw']}")
            if not m.get("stillThere"):
                problems.append("Stop syncing vanished after the menu was used — a binding died")
            if not m["emoji"] and not m.get("threw") and m.get("stillThere"):
                print("  the menu opens, its rows act, and the card survives it")
    finally:
        t.stop()
    if problems:
        for p in problems:
            print("FAIL ", p)
        return 1
    print("PASS  folder sync card: order, flat icons, one size, working menu")
    return 0


def main():
    try:
        urllib.request.urlopen(BASE + "/client", timeout=5)
    except Exception as e:
        print(f"SKIP  no instance at {BASE} ({e})")
        return 2
    return asyncio.run(drive(BASE + "/client"))


if __name__ == "__main__":
    sys.exit(main())
