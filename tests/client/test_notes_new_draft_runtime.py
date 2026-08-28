"""A relay repaint must not close a brand-new Notes editor before its first keystroke."""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest
import websockets


ROOT = Path(__file__).resolve().parents[2]
CHROME = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
          or shutil.which("chromium") or "/opt/google/chrome/google-chrome")


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/notes.js":
            body = (ROOT / "static/js/client/notes.js").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
        else:
            body = b"""<!doctype html><meta charset=utf-8><div id=feed></div><script>
window.__refreshResolve=null; window.__published=[];
window.__PC={VIEW:'notes',ME:{pubkey:'me'},
  $:(s,r=document)=>r.querySelector(s), $$:(s,r=document)=>[...r.querySelectorAll(s)],
  enc:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
  toast:()=>{}, uiConfirm:async()=>true, uiPrompt:async()=>'', modal:()=>{}, closeModal:()=>{},
  mdToHtml:s=>String(s||''), nip44dec:async(_pk,s)=>s, nip44enc:async(_pk,s)=>s,
  publish:async(kind,content,tags)=>{const ev={kind,content,tags,created_at:123,id:'saved'};
    window.__published.push(ev); return {ok:true,ev};}
};
window.Store={query:()=>[],saveEvent:()=>{}};
window.Relay={query:()=>new Promise(r=>window.__refreshResolve=r),subscribe:()=>null,close:()=>{}};
</script><script src=/notes.js></script>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def _drive(ws_url):
    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        seq = 0

        async def call(method, params=None):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == seq:
                    assert "error" not in msg, msg
                    return msg["result"]

        await call("Runtime.enable")
        for _ in range(100):
            ready = await call("Runtime.evaluate", {"expression": "document.readyState",
                                                     "returnByValue": True})
            if ready.get("result", {}).get("value") == "complete":
                break
            await asyncio.sleep(.02)
        async def evaluate(expression):
            out = await call("Runtime.evaluate", {"expression": expression,
                                                   "returnByValue": True})
            remote = out["result"]
            assert remote.get("subtype") != "error", remote.get("description")
            return remote.get("value")

        for _ in range(100):
            if await evaluate("!!window.PCNotes"):
                break
            await asyncio.sleep(.02)
        assert await evaluate("!!window.PCNotes"), "Notes module did not boot"
        await evaluate("PCNotes.render(); true")
        for _ in range(100):
            if await evaluate("!!document.querySelector('.nt-new')"):
                break
            await asyncio.sleep(.02)
        assert await evaluate("!!document.querySelector('.nt-new')")
        assert await evaluate("document.querySelector('.nt-new').click(); !!document.querySelector('.nt-title')")
        # Deliver a genuine library change through the still-running refresh. This calls _paint in
        # the exact gap where the new note has no signed event and therefore is not in `_lib`.
        await evaluate("window.__refreshResolve([{created_at:50,content:JSON.stringify({title:'remote',body:'x'}),"
                       "tags:[['d','pcai:note:remote'],['l','pcai-notes']]}]); true")
        await asyncio.sleep(.08)
        assert await evaluate("!!document.querySelector('.nt-title') && !!document.querySelector('.nt-body')"
                              " && !!document.querySelector('.nt-wrap.nt-open')"), \
            "relay repaint closed the unsaved editor"
        await evaluate("(()=>{const t=document.querySelector('.nt-title'),b=document.querySelector('.nt-body');"
                       "t.value='survives';t.dispatchEvent(new Event('input',{bubbles:true}));"
                       "b.value='kept';b.dispatchEvent(new Event('input',{bubbles:true}));return true})()")
        await asyncio.sleep(.8)
        return json.loads(await evaluate("JSON.stringify({open:!!document.querySelector('.nt-wrap.nt-open'),"
            "title:document.querySelector('.nt-title').value,body:document.querySelector('.nt-body').value,"
            "published:window.__published.length})"))


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")
def test_new_note_survives_live_refresh_and_first_save():
    web_port, debug_port = _port(), _port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tempfile.mkdtemp(prefix="pc-notes-draft.")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
        f"--user-data-dir={profile}", f"--remote-debugging-port={debug_port}",
        f"http://127.0.0.1:{web_port}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pages = None
        for _ in range(100):
            try:
                pages = json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{debug_port}/json/list", timeout=1))
                pages = [p for p in (pages or []) if p.get("type") == "page"
                         and p.get("url", "").startswith(f"http://127.0.0.1:{web_port}/")]
                if pages:
                    break
            except Exception:
                time.sleep(.03)
        assert pages, "Chrome debugging endpoint did not start"
        got = asyncio.run(_drive(pages[0]["webSocketDebuggerUrl"]))
        assert got == {"open": True, "title": "survives", "body": "kept", "published": 1}
    finally:
        chrome.terminate()
        with contextlib.suppress(Exception):
            chrome.wait(timeout=5)
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
