"""Files → This Computer: cut, copy, paste and move, by mouse, keyboard, drag and touch.

Reported as "File Manager is missing basic file operations like cut, copy, paste, move" and "I don't
see move for My Computer files". Cut, Copy and Paste existed -- as toolbar buttons that appeared only
AFTER a selection, which needs Ctrl+click or the small tick box, and right-click toggled the
selection instead of opening a menu. So the operations were there and could not be found.

Now every operation lives in ONE list, reached from right-click (a long-press on a phone fires the
same event), from `Actions ▾` beside the selection, and from the keyboard -- and the toolbar holds
navigation and selection only, because the fix for "can't find it" is not eleven buttons.

This runs the SHIPPED hostfiles.js and client.css in headless Chrome with real input through CDP, over
a fake disk that answers like desktop/hostfs.js (tests/test_host_fs.py covers the real one).
"""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest
import websockets

from tests.client.test_notes_new_draft_runtime import CHROME, ROOT, _port

PAGE = r"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel=stylesheet href="/static/css/client.css">
<body class="dark"><div id="pane" style="padding:8px"></div>
<script>
/* A disk. Paths are absolute; a folder is {dir:true}, a file {size}. transfer/rename/trash/mkdir
 * follow desktop/hostfs.js: collisions refused, copy into the same folder makes "name (copy)". */
const FS = {'/home/u':{dir:true}, '/home/u/Docs':{dir:true}, '/home/u/Pics':{dir:true},
            '/home/u/a.txt':{size:1}, '/home/u/b.txt':{size:2}, '/home/u/Docs/old.txt':{size:3}};
window.__calls = []; window.__pick = '/home/u/Pics';
const base = p => p.split('/').pop(), dirOf = p => p.slice(0, p.lastIndexOf('/')) || '/';
const under = (p) => Object.keys(FS).filter(k => k === p || k.startsWith(p + '/'));
window.pcHost = {
  list: async (d) => ({ parent: d === '/' ? null : dirOf(d), entries: Object.keys(FS)
      .filter(k => k !== d && dirOf(k) === d)
      .map(k => ({ path:k, name:base(k), dir:!!FS[k].dir, size:FS[k].size||0, mtime:1 })) }),
  roots: async () => [{kind:'home', path:'/home/u'}],
  transfer: async (items, dest, move) => {
    __calls.push(['transfer', items.slice(), dest, !!move]);
    for (const from of items) {
      let name = base(from);
      if (!move && dirOf(from) === dest) {
        const dot = name.lastIndexOf('.'), stem = dot > 0 ? name.slice(0, dot) : name, ext = dot > 0 ? name.slice(dot) : '';
        let i = 1; do { name = stem + (i === 1 ? ' (copy)' : ' (copy ' + i + ')') + ext; i++; } while (FS[dest + '/' + name]);
      }
      const to = dest + '/' + name;
      if (FS[to]) throw new Error('there is already something called ' + name);
      for (const k of under(from)) { FS[to + k.slice(from.length)] = FS[k]; if (move) delete FS[k]; }
    }
    return { moved: !!move };
  },
  mkdir: async (d, n) => { FS[d + '/' + n] = {dir:true}; },
  rename: async (f, t) => { __calls.push(['rename', f, t]); const to = dirOf(f) + '/' + t;
                            for (const k of under(f)) { FS[to + k.slice(f.length)] = FS[k]; delete FS[k]; } },
  trash: async (p) => { __calls.push(['trash', p]); for (const k of under(p)) delete FS[k]; },
  pickDirectory: async (o) => { __calls.push(['pick', o && o.title]); return window.__pick; },
  open: async (p) => { __calls.push(['open', p]); return {ok:true}; },
};
window.__FS = FS;
/* The Files screen's menu, reduced to what the menu CONTRACT is: [action,label,cls] rows, one
 * callback. Rendered as real buttons so the test clicks them like a person does. */
window.__menus = [];
function menu(anchor, items, onPick){
  document.querySelectorAll('.menu-pop').forEach(p => p.remove());
  const pop = document.createElement('div'); pop.className = 'menu-pop';
  __menus.push(items.map(i => i[0]));
  pop.innerHTML = items.map(([a, l]) => `<button data-m="${a}">${l}</button>`).join('');
  document.body.appendChild(pop);
  pop.querySelectorAll('[data-m]').forEach(b => b.onclick = () => { pop.remove(); onPick(b.dataset.m); });
}
window.__toasts = [];
window.UI = { view:'tiles', cmp: () => (a,b) => String(a.name).localeCompare(String(b.name)),
  fmtBytes: n => n + ' B', icon: () => '📄', folderIcon: () => '📁', typeName: () => 'File',
  openable: () => false, toast: t => __toasts.push(t), prompt: async () => 'renamed.txt',
  confirm: async () => true, menu, copy: async (t) => { __toasts.push('copied:' + t); } };
window.draw = () => PCHostFiles.render(document.getElementById('pane'), UI);
</script><script src="/static/js/client/hostfiles.js"></script>
<script>PCHostFiles.roots().then(() => { PCHostFiles.enter('/home/u'); return draw(); }).then(() => window.__ready = true);</script>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path.startswith("/static/"):
            f = ROOT / self.path.split("?")[0].lstrip("/")
            body = f.read_bytes()
            kind = "text/css" if f.suffix == ".css" else "text/javascript"
        else:
            body, kind = PAGE.encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Page:
    def __init__(self, ws):
        self.ws, self.seq = ws, 0

    async def call(self, method, params=None):
        self.seq += 1
        await self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.seq:
                assert "error" not in msg, msg
                return msg.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        assert "exceptionDetails" not in r, r
        return r["result"].get("value")

    async def until(self, expr, what):
        for _ in range(150):
            if await self.js(expr):
                return
            await asyncio.sleep(.02)
        raise AssertionError(what)

    async def centre(self, selector):
        return await self.js("(()=>{const r=document.querySelector(%s).getBoundingClientRect();"
                             "return [r.left+r.width/2,r.top+r.height/2]})()" % json.dumps(selector))

    async def click(self, selector, button="left"):
        x, y = await self.centre(selector)
        for kind in ("mousePressed", "mouseReleased"):
            await self.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": button,
                                                         "buttons": 0 if kind == "mouseReleased" else (2 if button == "right" else 1),
                                                         "clickCount": 1})
        await asyncio.sleep(.08)

    async def key(self, key, code, vk, ctrl=False):
        mods = 2 if ctrl else 0
        for kind in ("rawKeyDown", "keyUp"):
            await self.call("Input.dispatchKeyEvent", {"type": kind, "key": key, "code": code,
                                                       "windowsVirtualKeyCode": vk, "modifiers": mods})
        await asyncio.sleep(.08)

    async def pick(self, action):
        await self.until("!!document.querySelector('.menu-pop [data-m=%s]')" % json.dumps(action),
                         "the menu has no %r: %s" % (action, await self.js("JSON.stringify(__menus.slice(-1))")))
        await self.click(".menu-pop [data-m=%s]" % json.dumps(action))


CARD = ".file-card[data-p=%s]"


def card(p):
    return CARD % json.dumps(p)


async def _scenario(page: Page):
    await page.until("window.__ready===true", "the pane never drew")
    got = {}

    # 1. The toolbar is short: no row of Cut/Copy/Move/Rename buttons, before or after a selection.
    got["bar_idle"] = await page.js("[...document.querySelectorAll('.fx-actions button')].map(b=>b.textContent.trim())")
    await page.click(card("/home/u/a.txt") + " .hf-select")
    got["bar_selected"] = await page.js("[...document.querySelectorAll('.fx-actions button')].map(b=>b.textContent.trim())")

    # 2. Actions ▾ → Cut, into Docs, Paste: a MOVE, and the cut is spent.
    await page.click(".hf-acts")
    got["actions_menu"] = await page.js("__menus[__menus.length-1]")
    await page.pick("cut")
    await page.until("!!document.querySelector('.hf-paste')", "no Paste button after Cut")
    got["paste_label"] = await page.js("document.querySelector('.hf-paste').textContent")
    await page.js("PCHostFiles.enter('/home/u/Docs'); draw()")
    await page.until("!!document.querySelector(%s)" % json.dumps(card("/home/u/Docs/old.txt")), "Docs never drew")
    await page.click(".hf-paste")
    await page.until("!!__FS['/home/u/Docs/a.txt']", "cut + paste did not move a.txt")
    got["after_move"] = await page.js("({src:!!__FS['/home/u/a.txt'], clip:!!document.querySelector('.hf-paste')})")

    # 3. Right-click a file → the menu (not a selection toggle) → Copy; Ctrl+V pastes a duplicate
    #    into the same folder, and the copy survives its paste.
    await page.click(card("/home/u/Docs/old.txt"), button="right")
    got["context_menu"] = await page.js("__menus[__menus.length-1]")
    await page.pick("copy")
    await page.key("v", "KeyV", 86, ctrl=True)
    await page.until("!!__FS['/home/u/Docs/old (copy).txt']", "Ctrl+V did not paste a duplicate")
    got["copy_kept"] = await page.js("!!document.querySelector('.hf-paste')")

    # 4. Keyboard shortcuts stay out of a text box.
    await page.js("(()=>{const i=document.createElement('input');i.id='typing';document.body.appendChild(i);i.focus()})()")
    before = await page.js("Object.keys(__FS).length")
    await page.key("v", "KeyV", 86, ctrl=True)
    got["paste_while_typing"] = (await page.js("Object.keys(__FS).length")) - before
    await page.js("document.getElementById('typing').remove()")

    # 5. Ctrl+X by keyboard on a selection, then Escape forgets the clipboard.
    await page.js("PCHostFiles.enter('/home/u'); draw()")
    await page.until("!!document.querySelector(%s)" % json.dumps(card("/home/u/b.txt")), "home never drew")
    await page.click(card("/home/u/b.txt") + " .hf-select")
    await page.key("x", "KeyX", 88, ctrl=True)
    got["cut_by_key"] = await page.js("document.querySelector('.hf-paste')?.textContent||''")
    await page.key("Escape", "Escape", 27)
    got["escape_clears"] = await page.js("!document.querySelector('.hf-paste')")

    # 6. Move to… asks the machine's folder chooser, with a title that says what it is for.
    await page.click(card("/home/u/b.txt"), button="right")
    await page.pick("moveTo")
    await page.until("!!__FS['/home/u/Pics/b.txt']", "Move to… did not move b.txt")
    got["picker_title"] = await page.js("__calls.filter(c=>c[0]==='pick').map(c=>c[1])")

    # 7. Drag a file onto a folder: a move.
    await page.js("""(()=>{const src=document.querySelector(%s), dst=document.querySelector(%s);
        const dt=new DataTransfer();
        src.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
        dst.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:dt}));
        dst.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt}));})()"""
                  % (json.dumps(card("/home/u/Pics")), json.dumps(card("/home/u/Docs"))))
    await page.until("!!__FS['/home/u/Docs/Pics/b.txt']", "dropping a folder onto a folder did not move it")

    # 8. Right-click on empty space: paste / new folder / select all, not a file's menu.
    await page.js("__menus=[]")
    x, y = await page.js("(()=>{const g=document.querySelector('#hf-grid').getBoundingClientRect();return [g.right-4,g.bottom-4]})()")
    for kind in ("mousePressed", "mouseReleased"):
        await page.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "right",
                                                     "buttons": 0 if kind == "mouseReleased" else 2, "clickCount": 1})
    await asyncio.sleep(.08)
    got["background_menu"] = await page.js("__menus[__menus.length-1]||null")
    await page.js("document.querySelectorAll('.menu-pop').forEach(p=>p.remove())")

    got["calls"] = await page.js("__calls")
    return got


async def _phone(page: Page):
    """A 390px phone: the bar must fit without pushing the page sideways."""
    await page.until("window.__ready===true", "the pane never drew")
    await page.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 3,
                                                           "mobile": True})
    await page.js("draw()")
    await page.until("!!document.querySelector('.fx-actions')", "no toolbar on the phone")
    await page.js("document.querySelector('.file-card .hf-select').click()")
    await page.until("!!document.querySelector('.hf-acts')", "no Actions button on the phone")
    return await page.js("""(()=>{const bar=document.querySelector('.fx-actions');
        const b=[...bar.querySelectorAll('button')].filter(x=>x.getClientRects().length);
        return {overflow: document.documentElement.scrollWidth > innerWidth + 1,
                buttons: b.length,
                offscreen: b.filter(x=>{const r=x.getBoundingClientRect();return r.right>innerWidth+1||r.left<-1}).length,
                tap: Math.min(...b.map(x=>x.getBoundingClientRect().height))}})()""")


def _run(fn):
    web_port, debug_port = _port(), _port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="pc-hostops.")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
        f"--user-data-dir={profile}", f"--remote-debugging-port={debug_port}", "--window-size=1280,900",
        f"http://127.0.0.1:{web_port}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pages = None
        for _ in range(150):
            try:
                pages = [p for p in json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{debug_port}/json/list", timeout=1))
                    if p.get("type") == "page" and p.get("url", "").startswith(f"http://127.0.0.1:{web_port}/")]
                if pages:
                    break
            except Exception:
                time.sleep(.05)
        assert pages, "Chrome debugging endpoint did not start"

        async def go():
            async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
                return await fn(Page(ws))
        return asyncio.run(go())
    finally:
        chrome.terminate()
        with contextlib.suppress(Exception):
            chrome.wait(timeout=5)
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)


needs_chrome = pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")


@needs_chrome
def test_every_file_operation_is_reachable_and_does_what_it_says():
    got = _run(_scenario)
    ops = {"cut", "copy", "moveTo", "copyTo", "trash"}
    # The bar: navigation and selection only -- every OPERATION is behind Actions ▾ / right-click.
    assert len(got["bar_selected"]) <= 6, got["bar_selected"]
    assert not {"Cut", "Copy", "Rename", "Move to…", "Move to trash"} & set(got["bar_selected"]), got["bar_selected"]
    assert any("Actions" in b for b in got["bar_selected"]), got["bar_selected"]
    # Actions ▾ and right-click offer the SAME operations.
    assert ops <= set(got["actions_menu"]), got["actions_menu"]
    assert ops <= set(got["context_menu"]) and "rename" in got["context_menu"], got["context_menu"]
    # Cut + Paste moved it, and a cut is spent by its paste.
    assert "1 item" in got["paste_label"] and got["paste_label"].startswith("Move"), got["paste_label"]
    assert got["after_move"] == {"src": False, "clip": False}, got["after_move"]
    # A copy is not spent: it can be pasted again.
    assert got["copy_kept"] is True
    assert got["paste_while_typing"] == 0, "Ctrl+V in a text box pasted files"
    assert got["cut_by_key"].startswith("Move 1 item"), got["cut_by_key"]
    assert got["escape_clears"] is True
    assert got["picker_title"] == ["Move to…"], got["picker_title"]
    assert got["background_menu"] and "newFolder" in got["background_menu"] and "cut" not in got["background_menu"], \
        got["background_menu"]
    # Every transfer was the right kind.
    kinds = [(c[2], c[3]) for c in got["calls"] if c[0] == "transfer"]
    assert kinds[:4] == [("/home/u/Docs", True), ("/home/u/Docs", False), ("/home/u/Pics", True),
                         ("/home/u/Docs", True)], kinds


@needs_chrome
def test_the_toolbar_fits_a_phone():
    got = _run(_phone)
    assert got["overflow"] is False, got
    assert got["offscreen"] == 0, got
    assert got["buttons"] <= 6, got
