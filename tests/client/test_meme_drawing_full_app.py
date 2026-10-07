"""MEME BUILDER DRAWING: a stroke drawn with a finger or a mouse is in the EXPORTED meme, where it was drawn.

Owner request (2026-10-06): "meme builder needs basic drawing tools and color pallet. Try to improve the
UI for simplicity. make mobile good too."

What a person sees, end to end, through the real client (templates/client.html + every client script):

  * the ✏️ Draw tool is on the builder's tool strip and opens a drawing bar with a colour palette;
  * a palette swatch picks the colour, and the current colour is shown;
  * a stroke drawn on the stage with REAL input (CDP touch at phone size, CDP mouse in the PosterChanOS
    window at desktop size -- both go through the browser's own gesture handling, unlike a synthetic
    PointerEvent) is in the RENDERED export at the place it was drawn, in that colour;
  * the eraser removes the drawing and ONLY the drawing -- the photo under it comes out intact;
  * undo puts the erased part back (in the export again), redo takes it away;
  * touch drawing never scrolls the page, and a two-finger gesture leaves no mark;
  * at 390px nothing scrolls sideways and every toolbar control is >=40px and on screen.

The export is the real one: `/client/meme/render` is answered by meme_builder_service.render -- the
actual ffmpeg filtergraph the server runs -- and every layer source (the photo, the uploaded drawing)
comes from a local media server the client uploads to with its ordinary Blossom PUT. Only the HTTP
and relay boundaries are fixtures (test_effects_full_app's).

Screenshots: set PC_MEME_SHOTS=<dir> (and optionally PC_MEME_SHOT_TAG=before|after).
"""
import asyncio
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets
from PIL import Image

from tests.client.test_effects_full_app import Browser, Handler, INIT

CHROME = '/opt/google/chrome/chrome'
PHOTO_RGB = (70, 110, 120)          # a muted teal no palette swatch is anywhere near
PHOTO_W, PHOTO_H = 720, 640         # the shape addLayer gives a new image layer on a 720x1280 canvas


class Media:
    """A Blossom-ish media server plus the meme renderer, on one local port."""

    def __init__(self):
        self.blobs = {}
        self.renders = []
        buf = io.BytesIO()
        Image.new('RGB', (PHOTO_W, PHOTO_H), PHOTO_RGB).save(buf, 'PNG')
        self.blobs['/photo.png'] = (buf.getvalue(), 'image/png')
        media = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, PUT, POST, OPTIONS, HEAD')
                self.send_header('Access-Control-Allow-Headers',
                                 'Authorization, Content-Type, X-Filename, X-Keep, X-No-Mirror')

            def _send(self, code, body=b'', ctype='application/json'):
                self.send_response(code)
                self._cors()
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)

            def do_OPTIONS(self):
                self._send(204)

            def do_GET(self):
                b = media.blobs.get(self.path.split('?')[0])
                if not b:
                    return self._send(404, b'{}')
                self._send(200, b[0], b[1])

            do_HEAD = do_GET

            def do_PUT(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                sha = hashlib.sha256(body).hexdigest()
                ctype = self.headers.get('Content-Type') or 'application/octet-stream'
                media.blobs['/' + sha + '.png'] = (body, ctype)
                url = media.base + '/' + sha + '.png'
                self._send(200, json.dumps({'url': url, 'sha256': sha, 'size': len(body), 'type': ctype}).encode())

            def do_POST(self):
                from app.services import meme_builder_service
                data = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                edit = data['edit']
                tmp = tempfile.mkdtemp(prefix='pc-meme-draw-src-')
                sources = {}
                for layer in edit.get('layers') or []:
                    for key in ('src', 'mask'):
                        u = layer.get(key)
                        if not u or layer.get('type') == 'text' or u in sources:
                            continue
                        path = '/' + u.split('/', 3)[-1] if u.startswith(media.base) else None
                        blob = media.blobs.get(path or '')
                        if not blob:
                            return self._send(400, json.dumps({'detail': 'could not fetch ' + u}).encode())
                        p = os.path.join(tmp, hashlib.sha256(u.encode()).hexdigest()[:16] + '.png')
                        Path(p).write_bytes(blob[0])
                        sources[u] = p
                try:
                    out, ctype = meme_builder_service.render(edit, sources)
                except Exception as e:  # the client shows the detail; the test reads it from there
                    return self._send(400, json.dumps({'detail': str(e)[:300]}).encode())
                media.renders.append((edit, out))
                self._send(200, out, ctype)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _route(media_base, os_mode):
    """Runs after the fixture INIT. Uploads go to the local media server (the user's chosen Blossom
    server, set the ordinary way), and the render is answered by it. XHR, not fetch: the fixture's
    fetch answers every '/upload' itself, and these must reach a real server."""
    return r'''
try{const s=JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}');
  Object.assign(s,{osMode:OSMODE,blossomEnabled:true,mediaServer:'MEDIA',mediaProto:'blossom'});
  localStorage.setItem('pc_nostr_settings',JSON.stringify(s));}catch(_){}
(function(){
  const viaXhr=(url,opts)=>new Promise((res,rej)=>{const x=new XMLHttpRequest();x.open(opts.method||'GET',url);
    x.responseType='arraybuffer';const h=opts.headers||{};for(const k of Object.keys(h)){try{x.setRequestHeader(k,h[k])}catch(_){}}
    x.onload=()=>res(new Response(x.status===204?null:x.response,{status:x.status,headers:{'Content-Type':x.getResponseHeader('Content-Type')||''}}));
    x.onerror=()=>rej(new TypeError('media server unreachable'));x.send(opts.body||null);});
  const inner=window.fetch;
  window.fetch=function(url,opts){opts=opts||{};const u=String(url);
    // The Meme Builder is an instance app: this account is a member here (what the gate asks).
    if(u.includes('/api/instance-welcome/access'))return Promise.resolve(new Response(JSON.stringify(
      {qualified:true,pubkey:window.__PC&&__PC.me()?__PC.me().pubkey:''}),{status:200,headers:{'Content-Type':'application/json'}}));
    if(u.includes('/client/meme/render'))return viaXhr('MEDIA/render',opts);
    if(u.startsWith('MEDIA'))return viaXhr(u,opts);
    return inner(url,opts);};
})();
'''.replace('MEDIA', media_base).replace('OSMODE', 'true' if os_mode else 'false')


async def _open(width, height, mobile, route, media, check):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-meme-draw-') as profile:
        proc = subprocess.Popen([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
                                 f'--window-size={width},{height}', '--remote-debugging-port=0',
                                 '--user-data-dir=' + profile, 'about:blank'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(150):
                if Path(profile, 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, 'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient(trust_env=False) as h:
                pages = (await h.get('http://127.0.0.1:' + port + '/json')).json()
            page = next(p for p in pages if p.get('type') == 'page')
            async with websockets.connect(page['webSocketDebuggerUrl'], max_size=40_000_000) as ws:
                b = Browser(ws)
                await b.call('Page.enable')
                await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',
                             {'width': width, 'height': height, 'deviceScaleFactor': 2 if mobile else 1,
                              'mobile': mobile})
                if mobile:
                    await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
                await b.call('Page.addScriptToEvaluateOnNewDocument',
                             {'source': INIT + _route(media.base, not mobile)})
                base = f'http://127.0.0.1:{server.server_port}/client'
                await b.call('Page.navigate', {'url': base})
                await b.until("!!window.__PC && !!window.NostrTools && document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);"
                           "document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);"
                           "document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me()')
                if route:
                    await b.call('Page.navigate', {'url': base + route})
                    await b.until('!!window.__PC && document.readyState==="complete" && !!__PC.me()')
                else:
                    await asyncio.sleep(.5)
                    await b.js("__PC.switchView('meme')")
                await b.until("!!document.getElementById('mb-stage') && !!window.PCMeme")
                await check(b)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            server.shutdown()
            server.server_close()


async def _shot(b, name):
    out = os.environ.get('PC_MEME_SHOTS')
    if not out:
        return
    import base64
    tag = os.environ.get('PC_MEME_SHOT_TAG', 'after')
    Path(out).mkdir(parents=True, exist_ok=True)
    data = (await b.call('Page.captureScreenshot', {'format': 'png'}))['data']
    Path(out, f'{tag}-{name}.png').write_bytes(base64.b64decode(data))


class Input:
    """Real input through CDP -- touch at phone size, mouse at desktop size."""

    def __init__(self, b, touch):
        self.b, self.touch = b, touch

    async def center(self, sel):
        r = await self.b.js(f"""(()=>{{const e=document.querySelector({json.dumps(sel)});if(!e)return null;
            e.scrollIntoView({{block:'nearest'}});const r=e.getBoundingClientRect();
            return [r.left+r.width/2, r.top+r.height/2]}})()""")
        assert r, f'{sel} is not on the page'
        return r

    async def tap(self, sel):
        x, y = await self.center(sel)
        await self.down(x, y)
        await self.up(x, y)
        await asyncio.sleep(.15)

    async def down(self, x, y):
        if self.touch:
            await self.b.call('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': x, 'y': y, 'id': 1}]})
        else:
            await self.b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
            await self.b.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y,
                                                          'button': 'left', 'buttons': 1, 'clickCount': 1})

    async def move(self, x, y):
        if self.touch:
            await self.b.call('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': x, 'y': y, 'id': 1}]})
        else:
            await self.b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y,
                                                          'button': 'left', 'buttons': 1})

    async def up(self, x, y):
        if self.touch:
            await self.b.call('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        else:
            await self.b.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y,
                                                          'button': 'left', 'buttons': 0, 'clickCount': 1})

    async def stroke(self, fx0, fx1, fy, steps=12):
        """A horizontal stroke across the STAGE, in fractions of the stage box."""
        r = await self.b.js("(()=>{const r=document.getElementById('mb-stage').getBoundingClientRect();"
                            "return [r.left,r.top,r.width,r.height]})()")
        pt = lambda fx: (r[0] + r[2] * fx, r[1] + r[3] * fy)
        await self.down(*pt(fx0))
        for i in range(1, steps + 1):
            await self.move(*pt(fx0 + (fx1 - fx0) * i / steps))
            await asyncio.sleep(.01)
        await self.up(*pt(fx1))
        await asyncio.sleep(.2)


def _px(png, x, y):
    im = Image.open(io.BytesIO(png)).convert('RGB')
    return im.getpixel((x, y)), im.size


def _near(got, want, tol=40):
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(got, want))


def _hex(c):
    c = c.lstrip('#')
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


async def _export(b, inp, media):
    """Render a still through the builder's own Render button and return the PNG bytes."""
    n = len(media.renders)
    await b.js("(()=>{const s=document.getElementById('mb-fmt');s.value='png';"
               "s.dispatchEvent(new Event('change',{bubbles:true}))})()")
    await b.until("!!document.getElementById('mb-render')")
    await inp.tap('#mb-render')
    for _ in range(300):
        if len(media.renders) > n:
            break
        err = await b.js("(document.querySelector('.mb-err')||{}).textContent||''")
        assert not err, err
        await asyncio.sleep(.1)
    assert len(media.renders) > n, 'the Render button never reached the renderer'
    return media.renders[-1][1]


DRAWBAR_UP = "(()=>{const d=document.getElementById('mb-drawbar');return !!d&&d.getBoundingClientRect().height>0})()"


async def _draw_mode(b, inp):
    """Into drawing mode through the tool strip (a phone hides the strip WHILE drawing, so only tap it
    when the drawing bar is not already up)."""
    if not await b.js(DRAWBAR_UP):
        await inp.tap('#mb-draw')
    await b.until(DRAWBAR_UP)


def _strokes():
    return ("(()=>{const p=JSON.parse(localStorage.getItem('pc_meme_project')||'{}');"
            "const l=(p.layers||[]).find(x=>x.draw);return l?l.draw.strokes.length:-1})()")


async def _drawing_round_trip(b, touch, label):
    media = _MEDIA[0]
    inp = Input(b, touch)
    await b.js("PCMeme.reset()")
    assert await b.js(f"PCMeme.addMedia({json.dumps(media.base + '/photo.png')},'image/png')")
    await b.until("(()=>{const i=document.querySelector('.mb-item img');return !!i&&i.complete&&i.naturalWidth>0})()")
    await asyncio.sleep(.4)
    await _shot(b, label + '-builder')

    await _draw_mode(b, inp)
    red = await b.js("(document.querySelector('#mb-drawbar .mb-sw[aria-label=\"Red\"]')||{}).dataset?.color||''")
    assert red, 'the drawing palette has no Red swatch'
    await inp.tap('#mb-drawbar .mb-sw[aria-label="Red"]')
    assert await b.js("document.querySelector('#mb-drawbar .mb-sw[aria-label=\"Red\"]').getAttribute('aria-pressed')==='true'"), \
        'picking a swatch does not show it as the current colour'
    assert (await b.js("document.getElementById('mb-dcolor').value")).lower() == red.lower(), \
        'the current-colour control does not show the picked colour'
    await b.js("(()=>{const s=document.getElementById('mb-dsize');s.value='30';s.dispatchEvent(new Event('input',{bubbles:true}))})()")

    scroll0 = await b.js("[scrollX,scrollY,document.getElementById('feed').scrollTop,document.scrollingElement.scrollTop]")
    await inp.stroke(0.2, 0.8, 0.5)
    assert await b.js(_strokes()) == 1, 'the stroke was not recorded on a drawing layer'
    if touch:
        assert await b.js("scrollX===0") and scroll0 == await b.js(
            "[scrollX,scrollY,document.getElementById('feed').scrollTop,document.scrollingElement.scrollTop]"), \
            'drawing with a finger scrolled the page'
        # Two fingers down mid-way: a pinch/scroll, never a mark.
        r = await b.js("(()=>{const r=document.getElementById('mb-stage').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
        p1 = {'x': r[0] + r[2] * .3, 'y': r[1] + r[3] * .3, 'id': 1}
        p2 = {'x': r[0] + r[2] * .6, 'y': r[1] + r[3] * .3, 'id': 2}
        await b.call('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [p1]})
        await b.call('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [p1, p2]})
        for k in range(1, 6):
            await b.call('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [
                dict(p1, y=p1['y'] + 12 * k), dict(p2, y=p2['y'] + 12 * k)]})
        await b.call('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        await asyncio.sleep(.2)
        assert await b.js(_strokes()) == 1, 'a two-finger gesture left a mark on the drawing'
    await _shot(b, label + '-drawn')

    # The eraser: a fat pass over the middle of the stroke only.
    await inp.tap('#mb-drawbar [data-dtool="eraser"]')
    await b.js("(()=>{const s=document.getElementById('mb-dsize');s.value='70';s.dispatchEvent(new Event('input',{bubbles:true}))})()")
    await inp.stroke(0.46, 0.54, 0.5, steps=6)
    assert await b.js(_strokes()) == 2

    out, cy = await _export(b, inp, media), 640
    (left, size), (mid, _), (photo, _), (bg, _) = (_px(out, 216, cy), _px(out, 360, cy),
                                                    _px(out, 360, 520), _px(out, 360, 100))
    assert size == (720, 1280), size
    assert _near(left, _hex(red)), f'the stroke is not in the export where it was drawn: {left} vs {red}'
    assert _near(mid, PHOTO_RGB, 24), f'the eraser did not take the stroke off -- or took the photo with it: {mid}'
    assert _near(photo, PHOTO_RGB, 24), f'the photo away from the stroke changed: {photo}'
    assert _near(bg, (0, 0, 0), 24), f'the canvas background changed: {bg}'

    # Undo the erase: the stroke is whole again, in the export too. Redo takes it back off.
    await _draw_mode(b, inp)
    await inp.tap('#mb-dundo')
    assert await b.js(_strokes()) == 1, 'undo did not take the eraser stroke back'
    out2 = await _export(b, inp, media)
    assert _near(_px(out2, 360, cy)[0], _hex(red)), 'after undo the export still shows the erased gap'
    await _draw_mode(b, inp)
    await inp.tap('#mb-dredo')
    assert await b.js(_strokes()) == 2, 'redo did not put the eraser stroke back'
    await _shot(b, label + '-final')
    assert not await b.js('__errors'), await b.js('__errors')


_MEDIA = []


@pytest.fixture
def media():
    m = Media()
    _MEDIA[:] = [m]
    yield m
    m.close()
    _MEDIA.clear()


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
@pytest.mark.parametrize('size', [(390, 844), (360, 740)])
def test_drawing_with_a_finger_reaches_the_export(media, size):
    async def check(b):
        await _drawing_round_trip(b, True, f'{size[0]}px')
        # Layout at phone width, in drawing mode and out of it.
        for mode in ('draw', 'tools'):
            if mode == 'draw':
                await _draw_mode(b, Input(b, True))
            else:
                await Input(b, True).tap('#mb-ddone')
                await b.until("!document.getElementById('mb-drawbar') || !document.getElementById('mb-drawbar').getBoundingClientRect().height")
            audit = await b.js("""(()=>{
              const vw=innerWidth, vh=innerHeight, out={overflow:document.documentElement.scrollWidth>vw+1, bad:[]};
              const st=document.getElementById('mb-stage').getBoundingClientRect();
              out.stage=[st.left,st.top,st.right,st.bottom];
              out.stageVisible = st.left>=-1 && st.top>=-1 && st.right<=vw+1 && st.bottom<=vh+1 && st.width>100;
              document.querySelectorAll('.mb-bar button, .mb-tools button, #mb-drawbar button, #mb-drawbar input, #mb-drawbar label.mb-swc, .mb-tabs button').forEach(e=>{
                if(e.checkVisibility && !e.checkVisibility()) return;
                if(e.type==='color' && e.closest('label.mb-swc')) return;       // the label is the target
                if(e.type==='range'){ const r=e.getBoundingClientRect();
                  if(r.left<-1||r.right>vw+1||r.bottom>vh+1||r.top<-1) out.bad.push([e.id||e.className,'offscreen',Math.round(r.left),Math.round(r.right)]); return; }
                const r=e.getBoundingClientRect();
                if(r.height<40||r.width<40) out.bad.push([e.id||e.getAttribute('aria-label')||e.className,'small',Math.round(r.width),Math.round(r.height)]);
                if(r.left<-1||r.right>vw+1||r.bottom>vh+1||r.top<-1) out.bad.push([e.id||e.getAttribute('aria-label')||e.className,'offscreen',Math.round(r.left),Math.round(r.top)]);
              });
              return out;})()""")
            assert not audit['overflow'], f'{size}: the page scrolls sideways ({mode})'
            assert audit['stageVisible'], f'{size}: the canvas is not fully on screen ({mode}): {audit["stage"]}'
            assert not audit['bad'], f'{size}: toolbar controls too small or off screen ({mode}): {audit["bad"]}'
            await _shot(b, f'{size[0]}px-layout-{mode}')
    asyncio.run(_open(size[0], size[1], True, '', media, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_drawing_with_a_mouse_in_the_posterchanos_window_reaches_the_export(media):
    async def check(b):
        await _drawing_round_trip(b, False, '1280px-window')
    asyncio.run(_open(1280, 800, False, '?pcwin=meme', media, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_the_palette_colours_a_caption_too(media):
    """One palette, not two: the text layer's colour control is the same swatch row."""
    async def check(b):
        inp = Input(b, False)
        await b.js("PCMeme.reset()")
        colours = "[...document.querySelector('SEL .mb-pal').querySelectorAll('.mb-sw')].map(s=>s.dataset.color)"
        await _draw_mode(b, inp)
        drawing = await b.js(colours.replace('SEL', '#mb-drawbar'))
        await inp.tap('#mb-ddone')
        await inp.tap('#mb-add-text')
        await b.until("!!document.querySelector('#mb-inspector .mb-pal')")
        assert len(drawing) >= 10 and await b.js(colours.replace('SEL', '#mb-inspector')) == drawing, \
            'the caption colour is not the drawing palette'
        await inp.tap('#mb-inspector .mb-pal .mb-sw[aria-label="Yellow"]')
        want = await b.js("document.querySelector('#mb-inspector .mb-sw[aria-label=\"Yellow\"]').dataset.color")
        got = await b.js("JSON.parse(localStorage.getItem('pc_meme_project')).layers.find(l=>l.type==='text').color")
        assert got.lower() == want.lower(), (got, want)
        await _shot(b, '1280px-caption-palette')
        css = await b.js("getComputedStyle(document.querySelector('.mb-item.mb-text')).color")
        assert css.replace(' ', '') == 'rgb(%d,%d,%d)' % _hex(want), css
    asyncio.run(_open(1280, 800, False, '', media, check))
