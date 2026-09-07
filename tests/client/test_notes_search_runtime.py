"""Exercise real Notes DOM/keyboard events: search must never replace its input or editor."""
import asyncio
import base64
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
from .test_notes_new_draft_runtime import CHROME, Handler, ROOT, _port


class NotesHandler(Handler):
    def do_GET(self):
        if self.path == '/client.css':
            body = (ROOT / 'static/css/client.css').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/css')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


async def drive(url, width):
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        seq = 0
        async def call(method, params=None):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps(dict(id=seq, method=method, params=params or {})))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get('id') == seq:
                    assert 'error' not in msg, msg
                    return msg['result']
        async def js(expression):
            result = await call('Runtime.evaluate', dict(expression=expression, returnByValue=True, awaitPromise=True))
            assert 'exceptionDetails' not in result, result
            return result['result'].get('value')
        await call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844, deviceScaleFactor=1, mobile=False))
        for _ in range(100):
            if await js('!!window.PCNotes'): break
            await asyncio.sleep(.03)
        await js("""(()=>{
          const css=document.createElement('link');css.rel='stylesheet';css.href='/client.css';document.head.append(css);
          document.body.style.cssText='margin:0;background:#101019;color:#eee';
          document.documentElement.style.cssText='--bg:#101019;--line:#30303e;--muted:#aaaabb;--neon:#a89aff;--accent-rgb:168,154,255';
          window.__queries=0;const query=Relay.query;Relay.query=(...a)=>{__queries++;return query(...a)};
          Store.query=()=>['Alpha travel','Beta ideas','Alpha recipes'].map((title,i)=>({created_at:10+i,content:JSON.stringify({title,body:'A useful thought for later.',updated:10+i,tags:['personal']}),tags:[['d','pcai:note:'+i],['l','pcai-notes']]}));
          PCNotes.render();return true;
        })()""")
        for _ in range(100):
            if await js("document.querySelectorAll('.nt-item').length===3"): break
            await asyncio.sleep(.03)
        assert await js("document.querySelectorAll('.nt-item').length===3")
        # A later gradient shorthand must not erase the drawer's opaque base: underlying note
        # titles otherwise show through its folder labels on mobile.
        assert await js("(()=>{const c=document.createElement('canvas').getContext('2d');c.fillStyle=getComputedStyle(document.querySelector('.nt-side')).backgroundColor;c.fillRect(0,0,1,1);return c.getImageData(0,0,1,1).data[3]===255})()"), 'Folder panel background is translucent'

        await js("window.__search=document.querySelector('.nt-search');__search.focus();window.__baseline=__queries;window.__changes=0;new MutationObserver(()=>__changes++).observe(document.querySelector('.nt-results'),{childList:true})")
        for char in 'alpha':
            await call('Input.insertText', {'text':char})
            await asyncio.sleep(.07)
            assert await js('document.activeElement===__search && __search.isConnected')
        assert await js('__changes===0'), 'Filtering ran before typing paused'
        await asyncio.sleep(.4)
        assert await js("document.querySelectorAll('.nt-item').length===2 && __changes===1 && __queries===__baseline")
        assert await js("document.activeElement===__search && __search.value==='alpha'")
        # Selection must survive a live-library repaint while the search owns focus.
        await js("__search.setSelectionRange(1,4);__refreshResolve([{created_at:55,content:JSON.stringify({title:'Alpha remote',body:'Synced',updated:55}),tags:[['d','pcai:note:remote'],['l','pcai-notes']]}]);true")
        await asyncio.sleep(.1)
        assert await js("document.activeElement===__search && __search.selectionStart===1 && __search.selectionEnd===4 && document.querySelectorAll('.nt-item').length===3")
        # Composition stays in the same element and doesn't filter intermediate characters.
        await js("__search.dispatchEvent(new CompositionEvent('compositionstart'));__search.value='beta';__search.dispatchEvent(new InputEvent('input',{isComposing:true}));true")
        await asyncio.sleep(.4)
        assert await js("document.querySelectorAll('.nt-item').length===3 && document.activeElement===__search"), await js("({count:document.querySelectorAll('.nt-item').length,focus:document.activeElement.className,connected:__search.isConnected})")
        await js("__search.dispatchEvent(new CompositionEvent('compositionend'));true")
        await asyncio.sleep(.4)
        assert await js("document.querySelectorAll('.nt-item').length===1 && document.querySelector('.nt-item b').textContent==='Beta ideas'")
        # Search must preserve a mounted editor, its mode and its text. On phones the editor hides
        # the list, so this programmatic search specifically checks the same shared update path.
        await js("document.querySelector('.nt-item').click();document.querySelector('.nt-preview').click();window.__body=document.querySelector('.nt-body');__body.value='Keep this edited thought';__body.dispatchEvent(new Event('input'));__search.value='alpha';__search.dispatchEvent(new Event('input'));true")
        await asyncio.sleep(.4)
        assert await js("document.querySelector('.nt-body')===__body && !__body.classList.contains('hidden') && __body.value==='Keep this edited thought'")
        # Return to the list and clear with the native search-input event.
        await js("document.querySelector('.nt-back').click();true")
        await asyncio.sleep(.15)
        await js("window.__search=document.querySelector('.nt-search');__search.focus();__search.value='';__search.dispatchEvent(new Event('input'));true")
        await asyncio.sleep(.4)
        assert await js("document.querySelectorAll('.nt-item').length===4 && document.activeElement===__search")
        assert await js("document.documentElement.scrollWidth<=innerWidth"), 'Notes overflow the viewport'
        await call('Page.enable')
        shot = await call('Page.captureScreenshot', {'format':'png'})
        Path(f'/tmp/pc-notes-ui-{width}.png').write_bytes(base64.b64decode(shot['data']))


@pytest.mark.parametrize('width', [390, 1280])
@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome is not installed')
def test_search_preserves_focus_editor_composition_and_local_filtering(width):
    server=http.server.ThreadingHTTPServer(('127.0.0.1',_port()),NotesHandler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    profile=tempfile.mkdtemp(prefix='pc-notes-search.')
    port=_port()
    chrome=subprocess.Popen([CHROME,'--headless=new','--no-sandbox','--disable-gpu',f'--user-data-dir={profile}',f'--remote-debugging-port={port}',f'http://127.0.0.1:{server.server_port}/'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        pages=[]
        for _ in range(100):
            try:
                pages=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list',timeout=1))
                pages=[p for p in pages if p.get('type')=='page']
                if pages: break
            except Exception: pass
            time.sleep(.03)
        assert pages
        asyncio.run(drive(pages[0]['webSocketDebuggerUrl'],width))
    finally:
        chrome.terminate()
        with contextlib.suppress(Exception): chrome.wait(timeout=5)
        server.shutdown()
        shutil.rmtree(profile,ignore_errors=True)
