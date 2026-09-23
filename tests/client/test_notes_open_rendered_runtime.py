"""Opening a note shows it RENDERED, including a large one whose body lives in a separate blob.

Reported as "opening notes should go to Preview so you see the nice Markdown". Notes already opened
rendered when `n.body` had text, but a note over the inline size keeps its body in an encrypted blob
(`bodyRef`) that is fetched on open -- so at the moment the editor was built `n.body` was '' and
the note opened as raw source. The notes most worth reading rendered were the ones that never were.

A brand-new note still opens in the editor: there is nothing to read yet.

Runs the shipped notes.js in headless Chrome with the harness from test_notes_new_draft_runtime.
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

PAGE = b"""<!doctype html><meta charset=utf-8><div id=feed></div><script>
window.__PC={VIEW:'notes',ME:{pubkey:'me'},
  $:(s,r=document)=>r.querySelector(s), $$:(s,r=document)=>[...r.querySelectorAll(s)],
  enc:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
  toast:()=>{}, uiConfirm:async()=>true, uiPrompt:async()=>'', modal:()=>{}, closeModal:()=>{},
  mdToHtml:s=>'<h1>'+String(s||'').replace(/^# /,'')+'</h1>', nip44dec:async(_pk,s)=>s, nip44enc:async(_pk,s)=>s,
  encFileUrl:async()=>'data:text/markdown,'+encodeURIComponent('# Big note'),
  publish:async(kind,content,tags)=>({ok:true,ev:{kind,content,tags,created_at:123,id:'saved'}})
};
const ev=(id,obj,at)=>({created_at:at,content:JSON.stringify(obj),tags:[['d','pcai:note:'+id],['l','pcai-notes']]});
window.__events=[ev('small',{title:'small',body:'# Small note'},60),
                 ev('big',{title:'big',body:'',bodyRef:'ab'.repeat(32)},50)];
window.Store={query:()=>window.__events,saveEvent:()=>{}};
window.Relay={query:async()=>window.__events,subscribe:()=>null,close:()=>{}};
</script><script src=/notes.js></script>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/notes.js":
            body, kind = (ROOT / "static/js/client/notes.js").read_bytes(), "text/javascript"
        else:
            body, kind = PAGE, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def _drive(ws_url):
    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        seq = 0

        async def evaluate(expression):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": "Runtime.evaluate",
                                      "params": {"expression": expression, "returnByValue": True}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == seq:
                    remote = msg["result"]["result"]
                    assert remote.get("subtype") != "error", remote.get("description")
                    return remote.get("value")

        async def until(expression, what):
            for _ in range(200):
                if await evaluate(expression):
                    return
                await asyncio.sleep(.02)
            raise AssertionError(what)

        await until("document.readyState==='complete' && !!window.PCNotes", "Notes module did not boot")
        await evaluate("PCNotes.render(); true")
        await until("document.querySelectorAll('.nt-item').length===2", "the library never listed")
        state = ("JSON.stringify({src:!document.querySelector('.nt-body').classList.contains('hidden'),"
                 "shown:document.querySelector('.nt-render').textContent})")
        opened = {}
        for nid in ("small", "big"):
            await evaluate("document.querySelector('.nt-item[data-id=\"%s\"]').click(); true" % nid)
            await until("!!document.querySelector('.nt-render') && /note/i.test("
                        "document.querySelector('.nt-render').textContent)", nid + " never rendered")
            opened[nid] = json.loads(await evaluate(state))
        await evaluate("document.querySelector('.nt-new').click(); true")
        opened["new"] = json.loads(await evaluate(state))
        return opened


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")
def test_existing_notes_open_rendered_and_a_new_one_opens_in_the_editor():
    web_port, debug_port = _port(), _port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="pc-notes-render.")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
        f"--user-data-dir={profile}", f"--remote-debugging-port={debug_port}",
        f"http://127.0.0.1:{web_port}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pages = None
        for _ in range(100):
            try:
                pages = [p for p in json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{debug_port}/json/list", timeout=1))
                    if p.get("type") == "page" and p.get("url", "").startswith(f"http://127.0.0.1:{web_port}/")]
                if pages:
                    break
            except Exception:
                time.sleep(.03)
        assert pages, "Chrome debugging endpoint did not start"
        got = asyncio.run(_drive(pages[0]["webSocketDebuggerUrl"]))
        assert got["small"] == {"src": False, "shown": "Small note"}, got
        assert got["big"] == {"src": False, "shown": "Big note"}, \
            "a note whose body is stored separately opened as raw source: %r" % got
        assert got["new"]["src"] is True, "a brand-new note must open in the editor: %r" % got
    finally:
        chrome.terminate()
        with contextlib.suppress(Exception):
            chrome.wait(timeout=5)
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
