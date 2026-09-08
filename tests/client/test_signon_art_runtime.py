"""Real sign-on template: artwork, mobile overflow, controls and disabled registration."""
import asyncio
import base64
import contextlib
import http.server
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

from jinja2 import Template
import pytest
import websockets
from .test_notes_new_draft_runtime import CHROME, ROOT, _port


class SignonHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/signon-test':
            html = Template((ROOT / 'templates/client.html').read_text()).render(
                ver='test', registration_enabled=False, secure=False, nostr_only=False)
            # Exercise the actual layout without a signer, relay, or registration side effects.
            html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
            html = html.replace('class="auth-gate hidden"', 'class="auth-gate"')
            body = html.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


async def drive(url, width, height, theme):
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
            got = await call('Runtime.evaluate', dict(expression=expression, returnByValue=True, awaitPromise=True))
            assert 'exceptionDetails' not in got, got
            return got['result'].get('value')
        await call('Emulation.setDeviceMetricsOverride', dict(width=width, height=height, deviceScaleFactor=1, mobile=False))
        for _ in range(100):
            if await js("document.readyState==='complete' && !!document.querySelector('.auth-hero-art')"): break
            await asyncio.sleep(.03)
        await js(f'document.documentElement.dataset.theme={json.dumps(theme)}')
        await js('document.fonts.ready')
        await js("document.querySelector('.auth-hero-art').decode()")
        await js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
        result = await js("""(()=>{
          const gate=document.querySelector('#auth-gate'), art=document.querySelector('.auth-hero-art');
          const box=art.getBoundingClientRect();
          const visible=el=>el.getClientRects().length>0;
          const buttons=[...gate.querySelectorAll('.btn')].filter(visible);
          return {loaded:art.complete&&art.naturalWidth===1916,
            aspect:box.width/box.height, gateOverflow:gate.scrollWidth>gate.clientWidth,
            viewportOverflow:document.documentElement.scrollWidth>innerWidth,
            buttons:buttons.map(el=>{const r=el.getBoundingClientRect();return {id:el.id,h:r.height,left:r.left,right:r.right}}),
            signupHidden:!visible(document.querySelector('#btn-show-signup')),
            bg:getComputedStyle(document.querySelector('.auth-card')).backgroundColor,
            ids:['btn-nip07','btn-amber','btn-nsec-login','btn-nip55'].map(id=>document.querySelectorAll('#'+id).length)};
        })()""")
        assert result['loaded'], result
        assert abs(result['aspect'] - 1916 / 821) < .02, result
        assert not result['gateOverflow'] and not result['viewportOverflow'], result
        assert result['signupHidden'] and result['ids'] == [1, 1, 1, 1], result
        assert result['bg'].startswith('rgb('), 'Sign-in controls need an opaque background'
        for button in result['buttons']:
            assert button['h'] >= 44 and button['left'] >= 0 and button['right'] <= width, button
        await call('Page.enable')
        shot = await call('Page.captureScreenshot', {'format': 'png'})
        Path(f'/tmp/pc-signon-{width}-{theme}.png').write_bytes(base64.b64decode(shot['data']))
        # Every existing pane must remain reachable by scrolling, without widening the phone.
        for pane in ['auth-amber', 'auth-signup', 'auth-conn']:
            assert await js(f"""(()=>{{
              document.querySelectorAll('.auth-pane').forEach(el=>el.classList.add('hidden'));
              const pane=document.getElementById('{pane}');pane.classList.remove('hidden');
              const gate=document.getElementById('auth-gate');
              const last=[...pane.querySelectorAll('button')].filter(el=>el.getClientRects().length).pop();
              last.scrollIntoView({{block:'end'}});
              const r=last.getBoundingClientRect();
              return gate.scrollWidth<=gate.clientWidth && r.bottom<=innerHeight+1 && r.top>=0;
            }})()"""), pane


@pytest.mark.parametrize('width,height,theme', [(320,640,'professional'),(390,844,'dark'),(844,390,'dark'),(1440,1000,'dark'),(1440,1000,'professional')])
@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome is not installed')
def test_signon_art_and_controls_fit_without_exposing_closed_signup(width, height, theme):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', _port()), SignonHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix='pc-signon.')
    port = _port()
    chrome = subprocess.Popen([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu', f'--user-data-dir={profile}', f'--remote-debugging-port={port}', f'http://127.0.0.1:{server.server_port}/signon-test'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pages = []
        for _ in range(100):
            try:
                pages = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=1))
                pages = [p for p in pages if p.get('type') == 'page']
                if pages: break
            except Exception:
                pass
            time.sleep(.03)
        assert pages
        asyncio.run(drive(pages[0]['webSocketDebuggerUrl'], width, height, theme))
    finally:
        chrome.terminate()
        with contextlib.suppress(Exception): chrome.wait(timeout=5)
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
