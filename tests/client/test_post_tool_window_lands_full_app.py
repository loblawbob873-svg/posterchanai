"""A TOOL WINDOW OPENED FOR A POST DOES ITS JOB ON THE FIRST CLICK -- asserted on what is on screen.

On PosterChanOS, 🎞️ Meme Builder and 🎬 Effect on a post open in their OWN window
(`?pcwin=meme&pcpost=<id>`, `?pcwin=ai&pcpost=<id>`). Reported after that shipped:

  * "effects not working from post hamburger menu now, ai chat opens but no effects dialog"
  * "meme builder -- i have to do it twice for the image to appear on the video timeline"

The tests that shipped with it (test_post_tools_open_their_own_window_full_app.py) asserted that the
desktop was ASKED to open the window and that a "added to the Meme Builder" TOAST appeared -- so a
window that toasted success and showed nothing passed. These assert the two things a person sees:
the Effects studio sheet is open, and the post's image is a clip on the Meme Builder's timeline --
still there after the builder's own first render has had time to run.

Each window is a FRESH document, booted already signed in (the shell shares the session), exactly as
the shell opens it. Only HTTP and WebSocket are fixtures (test_effects_full_app's).
"""
import asyncio
import json
import threading
import tempfile
import subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, INIT

CHROME = '/opt/google/chrome/chrome'
# The fixture relay answers from __events; a reloaded document gets them back from localStorage.
PERSIST = "window.__events=JSON.parse(localStorage.getItem('__fixtureEvents')||'[]');"


async def _tool_window(view):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    got = {}
    with tempfile.TemporaryDirectory(prefix='pc-tool-window-') as profile:
        proc = subprocess.Popen([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=1280,900',
                                 '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, 'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get('http://127.0.0.1:' + port + '/json')).json()
            page = next(p for p in pages if p.get('type') == 'page' and p.get('url') == 'about:blank')
            async with websockets.connect(page['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call('Page.enable')
                await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
                await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': INIT + PERSIST})
                base = f'http://127.0.0.1:{server.server_port}/client'
                # Sign in once, with a real signed post carrying an image -- as the Social window would.
                await b.call('Page.navigate', {'url': base})
                await b.until("!!window.__PC && !!window.NostrTools && document.body.classList.contains('guest')")
                post_id = await b.js('''(()=>{const key=new Uint8Array(32).fill(1);
                  const ev=NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000),
                    content:'a picture '+location.origin+'/fixture.png',tags:[]},key);
                  localStorage.setItem('__fixtureEvents',JSON.stringify([ev]));window.__events=[ev];
                  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);
                  document.querySelector('#btn-nsec-login').click();return ev.id})()''')
                await b.until('!!__PC.me()')
                await asyncio.sleep(1.0)
                # The tool window, the way the shell opens it: a fresh document naming the post.
                await b.call('Page.navigate', {'url': f'{base}?pcwin={view}&pcpost={post_id}'})
                await b.until('!!window.__PC && document.readyState==="complete"')
                await b.until('!!__PC.me()')
                if view == 'ai':
                    for _ in range(80):
                        if await b.js("!!document.querySelector('#fxs-close')"):
                            break
                        await asyncio.sleep(.25)
                    got['studio'] = await b.js("!!document.querySelector('#fxs-close')")
                    got['attached'] = await b.js("document.querySelectorAll('.ai-attach img, .ai-attach .att, #ai-attach *').length")
                else:
                    for _ in range(60):
                        if await b.js("document.querySelectorAll('.mb-clip').length>0"):
                            break
                        await asyncio.sleep(.25)
                    await asyncio.sleep(3.0)          # the builder's own first render and project restore
                    got['clips'] = await b.js("document.querySelectorAll('.mb-clip').length")
                got['errors'] = await b.js('__errors')
        finally:
            proc.terminate()
            server.shutdown()
    return got


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_an_effect_window_opens_the_effects_studio_on_the_first_click():
    got = asyncio.run(_tool_window('ai'))
    assert got['studio'], "the AI chat opened but the Effects studio never did: %r" % got


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_a_meme_builder_window_puts_the_posts_image_on_the_timeline_on_the_first_click():
    got = asyncio.run(_tool_window('meme'))
    assert got['clips'] >= 1, "the Meme Builder opened with nothing on its timeline: %r" % got
