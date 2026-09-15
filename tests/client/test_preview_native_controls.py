"""File previews must remain clickable below a native Files window's title bar."""
import asyncio
import json
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser

ROOT = Path(__file__).resolve().parents[2]


def blank_pdf():
    """One real blank PDF page, built locally without downloads or extra PDF libraries."""
    objects = ['<</Type/Catalog/Pages 2 0 R>>',
               '<</Type/Pages/Kids[3 0 R]/Count 1>>',
               '<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>']
    data, offsets = '%PDF-1.4\n', []
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{number} 0 obj\n{body}\nendobj\n'
    start = len(data)
    data += 'xref\n0 4\n0000000000 65535 f \n'
    data += ''.join(f'{offset:010d} 00000 n \n' for offset in offsets)
    return data + f'trailer\n<</Size 4/Root 1 0 R>>\nstartxref\n{start}\n%%EOF'


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith('/fixture'):
            html = b'''<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css">
<script src="/static/js/client/oswin.js"></script>
<script src="/static/js/client/preview.js"></script>
<div id="files-fixture">Files remain open</div>
<script>window.__saved=[];window.__PC={saveBlobAs:async(blob,name)=>__saved.push({name,size:blob.size})};
PCOSWin.adopt();</script>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(html)
        else:
            super().do_GET()


async def check_controls(tmp_path, kind, native, scale):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc = subprocess.Popen([
        '/opt/google/chrome/chrome', '--headless=new', '--no-sandbox', '--disable-gpu',
        '--remote-debugging-port=0', '--user-data-dir=' + str(tmp_path), 'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if (tmp_path / 'DevToolsActivePort').exists():
                break
            await asyncio.sleep(.1)
        port = (tmp_path / 'DevToolsActivePort').read_text().splitlines()[0]
        async with httpx.AsyncClient() as client:
            pages = (await client.get(f'http://127.0.0.1:{port}/json')).json()
        async with websockets.connect(next(p['webSocketDebuggerUrl'] for p in pages if p['type'] == 'page')) as ws:
            browser = Browser(ws)
            await browser.call('Page.enable')
            await browser.call('Network.enable')
            await browser.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
            await browser.call('Emulation.setDeviceMetricsOverride', {
                'width': 600 if scale < 1 else 1100, 'height': 720,
                'deviceScaleFactor': 1, 'mobile': False,
            })
            await browser.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/fixture' + ('?pcwin=files' if native else '')})
            for _ in range(100):
                if await browser.js('!!window.PCPreview && document.readyState === "complete"'):
                    break
                await asyncio.sleep(.05)
            await browser.js(f'document.documentElement.style.setProperty("--ui-scale",{json.dumps(str(scale))})')
            # Both paths execute the shipped renderer and action handlers, including vendored pdf.js.
            mime = 'image/svg+xml' if kind == 'image' else 'application/pdf'
            name = 'picture.svg' if kind == 'image' else 'document.pdf'
            data = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="red"/></svg>' if kind == 'image' else blank_pdf()
            assert await browser.js(f'PCPreview.open({{name:{json.dumps(name)},mime:{json.dumps(mime)},blob:new Blob([{json.dumps(data)}],{{type:{json.dumps(mime)}}})}})')
            if kind == 'pdf':
                for _ in range(100):
                    if await browser.js('!!document.querySelector(".pv-pdf-page")'):
                        break
                    await asyncio.sleep(.05)
                assert await browser.js('!!document.querySelector(".pv-pdf-page")')
            geometry = await browser.js('''(()=>{const bar=document.querySelector('#pc-oswin-chrome');
              const bounds=bar?.getBoundingClientRect();return Array.from(document.querySelectorAll('.pv-acts button'),b=>{
                const r=b.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
                return {name:b.className,clear:!bounds||r.top>=bounds.bottom-.1,
                  clickable:b.contains(document.elementFromPoint(x,y)),x,y};})})()''')
            assert geometry and all(row['clear'] and row['clickable'] for row in geometry), geometry
            if not native:
                assert await browser.js('document.querySelector(".pv-sheet").getBoundingClientRect().top') == 0

            async def click(selector):
                point = await browser.js(f'''(()=>{{const r=document.querySelector({json.dumps(selector)}).getBoundingClientRect();return {{x:r.x+r.width/2,y:r.y+r.height/2}}}})()''')
                for event in ('mousePressed', 'mouseReleased'):
                    await browser.call('Input.dispatchMouseEvent', {'type': event, **point, 'button': 'left', 'clickCount': 1})

            if kind == 'image':
                await click('.pv-zoom')
                assert await browser.js('document.querySelector(".pv-img").classList.contains("pv-actual")')
                await click('.pv-rot')
                assert await browser.js('document.querySelector(".pv-img").style.transform === "rotate(90deg)"')
            await click('.pv-dl')
            for _ in range(50):
                if await browser.js('__saved.length === 1'):
                    break
                await asyncio.sleep(.02)
            assert await browser.js('__saved[0].name') == name
            await click('.pv-x')
            assert await browser.js('!PCPreview.isOpen() && !document.querySelector(".pv-sheet") && !!document.querySelector("#files-fixture")')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('kind', ['image', 'pdf'])
@pytest.mark.parametrize('native,scale', [(True, 1), (True, .67), (False, 1)])
def test_preview_toolbar_is_clickable_below_native_chrome(tmp_path, kind, native, scale):
    asyncio.run(check_controls(tmp_path, kind, native, scale))
