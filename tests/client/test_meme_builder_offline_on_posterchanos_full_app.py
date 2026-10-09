"""POSTERCHANOS, NO NETWORK: THE MEME BUILDER STILL MAKES A MEME, ON THE MACHINE.

"for PosterChanOS, most of memebuilder should process on that machine (minus the AI features that need
network)" and "Users should be able to access their … meme builder … without network". Before: every
picture added to the builder was uploaded to Blossom FIRST (the server renderer can only fetch URLs), so
with no network a dropped photo failed and nothing could be made.

The real client and the real builder, offline (navigator.onLine false, and the Blossom server and the
instance's renderer both unreachable). The desktop bridge `pcMemeLocal` is the SHIPPED
desktop/meme-local.js, reached through a small local server that plays the desktop main process: it
keeps the dropped file on disk, serves it at the address the bridge returned, and renders with the
server's own renderer. A photo is dropped on the stage and Render makes a still: the layer must be the
kept file, the still must contain the photo, and the instance's renderer must never have been asked.
"""
import asyncio
import base64
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from tests.client.test_meme_drawing_full_app import CHROME, PHOTO_RGB, Input, _near, _open, media  # noqa: F401
from tests.test_meme_local_render import CLI, NODE, ROOT


class Machine:
    """The desktop main process, as far as the builder can tell."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='pc-meme-machine-')
        self.renders = []
        m = self

        def node(job):
            job.update(dir=m.dir, app=str(ROOT), python=sys.executable)
            out = subprocess.run([NODE, '-e', CLI, str(ROOT / 'desktop/meme-local.js')], input=json.dumps(job),
                                 capture_output=True, text=True, timeout=300)
            assert out.returncode == 0, out.stderr[-2000:]
            return json.loads(out.stdout)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b'', ctype='application/json'):
                self.send_response(code)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._send(204)

            def do_GET(self):
                f = node({'op': 'local', 'ref': self.path})['file']
                if not f:
                    return self._send(404)
                self._send(200, Path(f).read_bytes(), 'image/png')

            def do_POST(self):
                job = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                if self.path == '/store':
                    r = node({'op': 'store', 'b64': job['b64'], 'name': job['name'], 'type': job['type']})
                    if r.get('ok'):
                        r['url'] = m.base + r['path']
                else:
                    r = node({'op': 'render', 'edit': job['edit'], 'sources': job['sources']})
                    m.renders.append((job['edit'], r))
                self._send(200, json.dumps(r).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


BRIDGE = r'''(base=>{
  const post=(path,body)=>new Promise((res,rej)=>{const x=new XMLHttpRequest();x.open('POST',base+path);
    x.setRequestHeader('Content-Type','application/json');x.onload=()=>res(JSON.parse(x.responseText));
    x.onerror=()=>rej(new TypeError('machine unreachable'));x.send(JSON.stringify(body));});
  const b64=ab=>{const u=new Uint8Array(ab);let s='';for(let i=0;i<u.length;i+=32768)s+=String.fromCharCode.apply(null,u.subarray(i,i+32768));return btoa(s);};
  window.pcMemeLocal={
    available:async()=>true,
    store:(bytes,name,type)=>post('/store',{b64:b64(bytes),name,type}),
    render:async(edit,sources)=>{const r=await post('/render',{edit,sources:sources.map(s=>({key:s.key,b64:b64(s.bytes)}))});
      if(!r.ok)return r;const bin=atob(r.b64),u=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
      return {ok:true,mime:r.mime,bytes:u};}};
})'''

OFFLINE = ("Object.defineProperty(Navigator.prototype,'onLine',{configurable:true,get:()=>false});"
           "window.dispatchEvent(new Event('offline'))")

DROP = r'''(async()=>{
  const c=document.createElement('canvas');c.width=320;c.height=240;const g=c.getContext('2d');
  g.fillStyle='rgb(%d,%d,%d)';g.fillRect(0,0,320,240);
  const blob=await new Promise(r=>c.toBlob(r,'image/png'));
  const dt=new DataTransfer();dt.items.add(new File([blob],'holiday.png',{type:'image/png'}));
  const wrap=document.querySelector('.mb-wrap');
  wrap.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt}));
  return true;})()''' % PHOTO_RGB


@pytest.mark.skipif(not Path(CHROME).exists() or NODE is None, reason='Chrome and node required')
@pytest.mark.parametrize('width', [1280, 390])
def test_offline_a_dropped_photo_is_kept_on_the_machine_and_rendered_there(media, width):
    machine = Machine()
    got = {}

    async def check(b):
        # The network is gone: Blossom and the instance's renderer are both unreachable.
        await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*', media.base + '/*']})
        await b.js(BRIDGE + '(' + json.dumps(machine.base) + ')')
        await b.js(OFFLINE)
        if width < 600:
            # Opened again with no network: the members' gate must let a remembered member in.
            await b.js("__PC.switchView('notes')")
            await asyncio.sleep(.5)
            await b.js("__PC.switchView('meme')")
            await b.until("!!document.getElementById('mb-stage')")
        await b.js("PCMeme.reset()")
        await b.js(DROP)
        await b.until("(()=>{const i=document.querySelector('.mb-item img');return !!i&&i.complete&&i.naturalWidth>0})()")
        got['src'] = await b.js("document.querySelector('.mb-item img').getAttribute('src')")
        await b.js("(()=>{const s=document.getElementById('mb-fmt');s.value='png';s.dispatchEvent(new Event('change',{bubbles:true}))})()")
        await b.until("!!document.getElementById('mb-render')")
        await Input(b, width < 600).tap('#mb-render')
        for _ in range(600):
            if machine.renders:
                break
            got['err'] = await b.js("(document.querySelector('.mb-err')||{}).textContent||''")
            if got['err']:
                break
            await asyncio.sleep(.1)
        await b.until("!!document.querySelector('#mb-result img, #mb-result video, #mb-result a')")

    try:
        asyncio.run(_open(width, 900, width < 600, '' if width < 600 else '?pcwin=meme', media, check))
        assert not got.get('err'), got
        assert '/__memelocal/' in got['src'] and got['src'].startswith(machine.base), (
            'the photo was not kept on the machine', got)
        assert machine.renders, 'the builder never asked the machine to render'
        edit, r = machine.renders[-1]
        assert r.get('ok'), r
        im = Image.open(io.BytesIO(base64.b64decode(r['b64']))).convert('RGB')
        assert _near(im.getpixel((im.width // 2, im.height // 2)), PHOTO_RGB), 'the still does not contain the photo'
        assert media.renders == [], 'the instance was asked to render'
    finally:
        machine.close()
