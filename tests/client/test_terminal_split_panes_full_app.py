"""Split view: two or more terminal tabs on screen at once, each a live shell of its own.

"terminal add way to split view two tabs or more or grid". Driven against the SHIPPED term.js +
client.css in a real Chrome, with the desktop's local PTY (`window.pcTerm`, desktop/preload.js)
stubbed the way preload injects it. Every stub shell echoes what it is sent, tagged with its own id,
and records the size it was last told -- so this measures what the person sees and what each PTY
was actually sent, not what term.js believes it did.

  split right   two panes side by side, two DIFFERENT shells; typing into each (after clicking it)
                reaches only that shell and shows only in that pane, and each PTY was told its own
                pane's grid, narrower than the single terminal's.
  grid          four panes in a 2x2 grid, four distinct live shells, each PTY at its pane's size.
  back to one   one pane again; the other shells keep running as tabs; the PTY is full size again.
  phone         at 390px the panes STACK (full width, one above the other), nothing scrolls the page
                sideways, and the phone key bar types into the FOCUSED pane.
  GPU loss      a WebGL context loss in any pane turns the GPU renderer off for this device: every
                pane drops it, and a terminal opened afterwards does not ask for WebGL again (an
                amdgpu page fault froze a PosterChanOS desktop for 21s with the Terminal focused).

PC_TERM_JS / PC_CLIENT_CSS point the page at another build -- the pre-split file, to prove these fail
without the change. PC_SHOT_DIR, when set, saves a screenshot of each layout there.
"""
import asyncio
import base64
import functools
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as full

ROOT = Path(__file__).resolve().parents[2]
CHROME = "/opt/google/chrome/chrome"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/vendor/xterm/xterm.css">
<link rel="stylesheet" href="/static/css/client.css">
<style>html,body{margin:0;height:100%} .app{display:block!important;height:100vh} .main{width:100%;max-width:none}</style>
</head><body class="term-view">
<div class="app" id="app"><main class="main" style="display:flex;flex-direction:column;height:100vh">
  <div id="feed" class="feed feed-term" style="flex:1;min-height:0"></div>
</main></div>
<script src="/static/vendor/xterm/xterm.js"></script>
<script src="/static/vendor/xterm/fit.js"></script>
<script>
window.__PC_API_BASE__ = '';
window.__errors = [];
addEventListener('error', e => __errors.push(String(e.message)));
try{ localStorage.clear(); }catch(_){}
/* A stand-in for xterm's WebGL addon: counts contexts made and let go, and keeps the loss handler
 * so the check can fire it the way a GPU reset would. It draws nothing -- the DOM renderer does. */
window.__gl = { made: 0, live: 0, lose: [] };
window.WebglAddon = class {
  constructor(){ __gl.made++; __gl.live++; this.gone = false; }
  activate(){} clearTextureAtlas(){}
  onContextLoss(fn){ __gl.lose.push(fn); return { dispose(){} }; }
  dispose(){ if(!this.gone){ this.gone = true; __gl.live--; } }
};
/* THE DESKTOP'S LOCAL PTY. Shell 1 is already running (a tab nobody has on screen). */
window.__log = { start: 0, writes: {}, size: {} };
(function(){
  const shells = new Map([['1', { seq: 0 }]]); let next = 2; const subs = new Set();
  const emit = (id, d) => { const s = shells.get(id); if(!s) return; s.seq += d.length;
    subs.forEach(f => f({ id, t: 'out', d, seq: s.seq })); };
  window.pcTerm = {
    start: async () => { __log.start++; const id = String(next++); shells.set(id, { seq: 0 });
      setTimeout(() => emit(id, 'shell' + id + '$ '), 20); return { id }; },
    write: async (id, d) => { id = String(id); (__log.writes[id] = __log.writes[id] || []).push(d);
      emit(id, d === '\r' ? '\r\nshell' + id + '$ ' : d === '\x03' ? '^C' : d); },
    resize: async (id, c, r) => { __log.size[String(id)] = [c, r]; return true; },
    backlog: async (id) => shells.has(String(id)) ? { d: '', seq: shells.get(String(id)).seq, alive: true } : null,
    close: async (id) => { shells.delete(String(id)); },
    list: async () => [...shells.keys()].map(id => ({ id, alive: true, idle: 5 })),
    attach: async () => true, detach: async () => true,
    onData: (fn) => { subs.add(fn); return () => subs.delete(fn); },
  };
})();
const $ = (s, r) => (r||document).querySelector(s);
const $$ = (s, r) => Array.from((r||document).querySelectorAll(s));
window.__PC = {
  $, $$, enc: (s) => String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  toast: () => {}, publish: async () => {}, ensureAiSession: async () => {},
  uiPrompt: async () => '', uiConfirm: async () => true, isView: (v) => v === 'terminal',
  viewer: () => ({ pubkey: 'a'.repeat(64) }),
  authFetch: async () => ({ ok: false, status: 503, json: async () => ({}) }),
};
/* What each pane SHOWS, read off the screen (xterm's DOM rows), in screen order. */
window.__paneText = () => $$('.tty-pane').map(p => (p.querySelector('.xterm-rows') || {}).textContent || '');
window.__paneRects = () => $$('.tty-pane').map(p => { const r = p.getBoundingClientRect();
  return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]; });
</script>
<script src="/static/js/client/term.js"></script>
<script>
(async () => {
  for(let i = 0; i < 100 && !window.PCTerm; i++) await new Promise(r => setTimeout(r, 20));
  await PCTerm.render();
  window.__ready = true;
})();
</script></body></html>"""


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def translate_path(self, path):
        path = path.split("?")[0].split("#")[0]
        if path == "/static/js/client/term.js" and os.environ.get("PC_TERM_JS"):
            return os.environ["PC_TERM_JS"]
        if path == "/static/css/client.css" and os.environ.get("PC_CLIENT_CSS"):
            return os.environ["PC_CLIENT_CSS"]
        if path.startswith("/static/"):
            return str(ROOT / path.lstrip("/"))
        return str(Path(self.directory) / (path.lstrip("/") or "index.html"))


class B(full.Browser):
    async def until(self, expression, tries=100):
        for _ in range(tries):
            if await self.js(expression):
                return
            await asyncio.sleep(.1)
        raise AssertionError({"waiting": expression, "errors": await self.js("__errors"),
                              "panes": await self.js("PCTerm._panes ? PCTerm._panes() : null"),
                              "log": await self.js("__log")})

    async def click(self, x, y):
        for kind in ("mousePressed", "mouseReleased"):
            await self.call("Input.dispatchMouseEvent", dict(type=kind, x=x, y=y, button="left", clickCount=1))

    async def click_sel(self, sel):
        r = await self.js(f"(()=>{{const e=document.querySelector({json.dumps(sel)});if(!e)return null;e.scrollIntoView({{block:'nearest',inline:'nearest'}});"
                          "const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()")
        assert r, f"{sel} is not on the screen"
        await self.click(*r)
        await asyncio.sleep(.15)

    async def click_pane(self, i):
        r = (await self.js("__paneRects()"))[i]
        await self.click(r[0] + r[2] / 2, r[1] + r[3] / 2)
        await asyncio.sleep(.15)

    async def shot(self, name):
        d = os.environ.get("PC_SHOT_DIR")
        if d:
            png = (await self.call("Page.captureScreenshot", {"format": "png"}))["data"]
            Path(d, name + ".png").write_bytes(base64.b64decode(png))


async def _session(tmp, width, height, mobile, body):
    (tmp / "index.html").write_text(PAGE)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(tmp)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-termsplit-", ignore_cleanup_errors=True) as prof:
        p = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                              "--remote-debugging-port=0", "--user-data-dir=" + prof, "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(150):
                if Path(prof, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(prof, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            url = next(x for x in pages if x["type"] == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(url, max_size=None) as ws:
                b = B(ws)
                await b.call("Page.enable")
                await b.call("Emulation.setDeviceMetricsOverride",
                             dict(width=width, height=height, deviceScaleFactor=1, mobile=mobile))
                if mobile:
                    await b.call("Emulation.setTouchEmulationEnabled", dict(enabled=True, maxTouchPoints=5))
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{srv.server_port}/index.html"})
                await b.until("window.__ready===true && !!(window.PCTerm && PCTerm.connected())")
                await asyncio.sleep(.4)
                return await body(b)
        finally:
            p.terminate()
            p.wait(timeout=10)
            srv.shutdown()


def _run(tmp, width, height, mobile, body):
    return asyncio.run(_session(tmp, width, height, mobile, body))


needs_chrome = pytest.mark.skipif(not Path(CHROME).exists() or not shutil.which("node"), reason="Chrome required")

PANES = "PCTerm._panes ? PCTerm._panes() : []"
ALL_CONNECTED = "(n=>{const p=PCTerm._panes?PCTerm._panes():[];return p.length===n&&p.every(x=>x.connected&&x.grid)})"


def _sid(pane):
    return pane["sid"].replace("local:", "")


async def _sizes_settled(b):
    """Every pane's PTY was last told exactly its pane's grid."""
    await b.until("(()=>{const p=PCTerm._panes();return p.every(x=>{const s=__log.size[x.sid.replace('local:','')];"
                  "return s&&x.grid&&s[0]===x.grid.cols&&s[1]===x.grid.rows})})()")


@needs_chrome
def test_split_right_grid_and_back_to_one(tmp_path):
    async def body(b):
        out = {}
        await _sizes_settled(b)
        single = (await b.js(PANES))[0]
        out["single"] = single
        # ── Split right: a second pane, side by side, showing the shell nobody had on screen.
        await b.click_sel("#tty-split")
        await b.click_sel("#tty-split-right")
        await b.until(ALL_CONNECTED + "(2)")
        await _sizes_settled(b)
        await asyncio.sleep(.3)
        panes = await b.js(PANES)
        rects = await b.js("__paneRects()")
        assert len({p["sid"] for p in panes}) == 2, panes
        assert rects[0][1] == rects[1][1] and rects[1][0] > rects[0][0] + rects[0][2] - 4, ("side by side", rects)
        for p in panes:
            assert p["grid"]["cols"] < single["grid"]["cols"] * 0.6, ("each pane is told its own, narrower size", p, single)
        await b.shot("desktop-split-right")
        # ── Type into each, by clicking it first.
        a, c = _sid(panes[0]), _sid(panes[1])
        await b.click_pane(1)
        await b.until("PCTerm._panes()[1].focused")
        await b.call("Input.insertText", {"text": "right-side"})
        await b.click_pane(0)
        await b.until("PCTerm._panes()[0].focused")
        await b.call("Input.insertText", {"text": "left-side"})
        await b.until("__paneText()[0].includes('left-side') && __paneText()[1].includes('right-side')")
        writes = await b.js("__log.writes")
        assert "".join(writes.get(c, [])) == "right-side", writes
        assert "".join(writes.get(a, [])) == "left-side", writes
        text = await b.js("__paneText()")
        assert "right-side" not in text[0] and "left-side" not in text[1], text
        # ── Grid: four live, distinct shells in two columns and two rows.
        await b.click_sel("#tty-split")
        await b.click_sel("#tty-split-grid")
        await b.until(ALL_CONNECTED + "(4)")
        await _sizes_settled(b)
        await asyncio.sleep(.3)
        panes = await b.js(PANES)
        rects = await b.js("__paneRects()")
        assert len({p["sid"] for p in panes}) == 4, panes
        assert len({r[0] for r in rects}) == 2 and len({r[1] for r in rects}) == 2, ("a 2x2 grid", rects)
        for p in panes:
            assert p["grid"]["rows"] < single["grid"]["rows"] * 0.6, ("each grid pane has its own height", p)
        await asyncio.sleep(.5)
        tabs = await b.js("[...document.querySelectorAll('#tty-sessions [data-tab]')].map(x=>x.dataset.tab)")
        assert len(tabs) == len(set(tabs)) and set(p["sid"] for p in panes) <= set(tabs), ("one tab per shell", tabs)
        saved = json.loads(await b.js("localStorage.getItem('pc_tty_layout')") or "null")
        assert saved and saved["mode"] == "grid" and saved["n"] == 4, saved
        await b.shot("desktop-grid")
        # ── Back to one pane: the other shells are still tabs; the PTY is full size again.
        await b.click_sel("#tty-split")
        await b.click_sel("#tty-pane-single")
        await b.until("PCTerm._panes().length===1")
        await _sizes_settled(b)
        one = (await b.js(PANES))[0]
        assert one["grid"]["cols"] == single["grid"]["cols"] and one["grid"]["rows"] == single["grid"]["rows"], (one, single)
        tabs = await b.js("[...document.querySelectorAll('#tty-sessions [data-tab]')].map(x=>x.dataset.tab)")
        assert len(tabs) >= 4, ("closing panes detaches, it does not end the shells", tabs)
        assert await b.js("localStorage.getItem('pc_tty_layout')") is None
        assert await b.js("__errors") == []
        return out
    _run(tmp_path, 1280, 800, False, body)


@needs_chrome
def test_split_on_a_phone_stacks_and_the_key_bar_types_into_the_focused_pane(tmp_path):
    async def body(b):
        await b.click_sel("#tty-split")
        await b.click_sel("#tty-split-right")          # side by side is not what a phone gets
        await b.until(ALL_CONNECTED + "(2)")
        await _sizes_settled(b)
        await asyncio.sleep(.3)
        rects = await b.js("__paneRects()")
        assert rects[0][0] == rects[1][0] and rects[0][2] == rects[1][2], ("stacked, same column", rects)
        assert rects[1][1] >= rects[0][1] + rects[0][3] - 4, ("one above the other", rects)
        assert rects[0][2] >= 370, ("full width on a 390px phone", rects)
        assert rects[0][3] >= 120 and rects[1][3] >= 120, ("each pane still has room for a shell", rects)
        assert not await b.js("document.documentElement.scrollWidth > innerWidth + 1"), "the page scrolls sideways"
        bar = await b.js("(()=>{const b=document.querySelector('.tty-bar').getBoundingClientRect();"
                         "return [...document.querySelectorAll('.tty-bar button')].filter(e=>{const r=e.getBoundingClientRect();"
                         "return r.width>0&&r.height>0}).every(e=>{const r=e.getBoundingClientRect();"
                         "return r.top>=b.top-0.5&&r.bottom<=b.bottom+0.5})})()")
        assert bar, "a bar control spills out of the bar at phone width"
        await b.shot("phone-split")
        panes = await b.js(PANES)
        top, bottom = _sid(panes[0]), _sid(panes[1])
        # The key bar's ^C goes to the pane that was tapped, and only to it.
        await b.click_pane(0)
        await b.until("PCTerm._panes()[0].focused")
        await b.js("__log.writes={}")
        await b.click_sel(".tty-keys [data-k='^C']")
        await b.until(f"(__log.writes[{json.dumps(top)}]||[]).includes('\\x03')")
        await b.click_pane(1)
        await b.until("PCTerm._panes()[1].focused")
        await b.click_sel(".tty-keys [data-k='Tab']")
        await b.until(f"(__log.writes[{json.dumps(bottom)}]||[]).includes('\\t')")
        writes = await b.js("__log.writes")
        assert writes.get(top) == ["\x03"] and writes.get(bottom) == ["\t"], writes
        await b.click_sel("#tty-split")
        await b.shot("phone-split-menu")
        assert await b.js("__errors") == []
    _run(tmp_path, 390, 800, True, body)


@needs_chrome
def test_a_lost_gpu_context_keeps_every_terminal_off_webgl(tmp_path):
    async def body(b):
        await b.until("__gl.made===1 && __gl.lose.length===1")
        await b.js("__gl.lose[0]()")                    # the GPU process reset under the terminal
        await b.until("__gl.live===0")
        # A terminal opened afterwards -- a fresh mount -- and a new pane: both stay on the DOM renderer.
        await b.js("PCTerm.unmount()")
        await b.js("PCTerm.render()")
        await b.until("PCTerm.connected()")
        await asyncio.sleep(1.0)
        assert await b.js("__gl.made") == 1, "a terminal opened after a GPU context loss asked for WebGL again"
        assert await b.js("__gl.live") == 0
        assert await b.js("Number(localStorage.getItem('pc_term_gl_lost'))>0"), "the loss was not recorded for this device"
    _run(tmp_path, 1280, 800, False, body)


@needs_chrome
def test_split_panes_hold_one_gpu_context_each_and_let_go_on_close(tmp_path):
    async def body(b):
        await b.until("__gl.made===1")
        await b.click_sel("#tty-split")
        await b.click_sel("#tty-split-grid")
        await b.until(ALL_CONNECTED + "(4)")
        await b.until("__gl.live===4")
        await asyncio.sleep(.5)
        assert await b.js("__gl.live") == 4, "more than one WebGL context per pane"
        await b.click_sel("#tty-split")
        await b.click_sel("#tty-pane-single")
        await b.until("PCTerm._panes().length===1")
        assert await b.js("__gl.live") == 1, "a closed pane kept its WebGL context"
    _run(tmp_path, 1280, 800, False, body)
