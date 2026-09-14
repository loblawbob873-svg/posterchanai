"""OPENING A POST MUST TAKE ONE CLICK, WHETHER OR NOT SOCIAL IS ALREADY OPEN.

Reported on the windowed desktop: "clicking on a reply on desktop has to be done twice now to bring
up the reply in Social", with the discriminating half supplied unprompted — "if social is not open,
it opens up the reply fine".

That pair is the whole specification. The post-opening path is the same call either way
(`openThread` → `PCOS.openDoc('post:<id>')` → `openApp`), so the only thing that differs is whether
a Social window already exists when it runs. A test that opens a post from a clean desktop passes
and says nothing about the bug; the state has to be set up first, which is exactly the class of
failure `feedback_test_the_state_the_code_ends_in` is about.

  cold    no Social window → the post opens on the first call
  warm    a Social window already open → the post STILL opens on the first call

Both drive the real client, the real desktop (PCOS) and the shipped renderers.

WHAT THIS DOES **NOT** COVER, AND WHY THE REPORTED BUG IS STILL OPEN. Both cases PASS here, so the
in-page window path is not where the double click comes from. On PosterChanOS a view is a REAL
COMPOSITOR TOPLEVEL (`PCOSWin.open`, the `_openedReal` branch in os.js `openApp`), and os.js says so
itself: "Invisible on the web, where PCOSWin is never enabled and this branch cannot run, which is
why every browser-driven check passes." A headless browser cannot make one, so this file guards the
half that works and is blind to the half that is broken. Closing that needs either a PCOSWin stub
driven through the same assertions, or a check that runs against the real desktop's shell. Kept
because the in-page path is a real regression surface — not because it proves the report is fixed.
"""
import asyncio
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, INIT

# What the desktop holds after one attempt to open a post.
PROBE = """(() => {
  // What matters is not that a frame appeared — it is whether the POST is in it. The first probe
  // measured window identity (data-view) and the client's VIEW, neither of which these frames
  // carry, so it reported the working cold case as broken too.
  const frames = [...document.querySelectorAll('.osw')];
  const post = frames.find(f => /Post/i.test((f.querySelector('.osw-title') || {}).textContent || ''));
  const text = post ? (post.innerText || '') : '';
  return {
    windows: frames.length,
    titles: frames.map(f => ((f.querySelector('.osw-title') || {}).textContent || '').trim()),
    postWindow: !!post,
    // the seeded note bodies are "reply test N"
    showsPost: /reply test/i.test(text),
    spinner: !!(post && post.querySelector('.spinner')),
    empty: post ? text.replace(/\s+/g, '').length < 40 : true,
    sample: text.replace(/\s+/g, ' ').slice(0, 180),
  };
})()"""


async def run(open_social_first):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix='pc-reply-click-')
    try:
        proc = subprocess.Popen(
            ['/opt/google/chrome/chrome', '--headless=new', '--no-sandbox', '--disable-gpu',
             '--window-size=1440,1000', '--remote-debugging-port=0',
             '--user-data-dir=' + profile, 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, 'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get('http://127.0.0.1:' + port + '/json')).json()
            tgt = next(p for p in pages if p.get('type') == 'page' and p.get('url') == 'about:blank')
            async with websockets.connect(tgt['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call('Page.enable')
                await b.call('Network.enable')
                await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',
                             {'width': 1440, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                await b.call('Page.addScriptToEvaluateOnNewDocument',
                             {'source': 'window.__hasChats=false;' + INIT})
                await b.call('Page.navigate',
                             {'url': 'http://127.0.0.1:%d/client' % server.server_port})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("""(()=>{const key=new Uint8Array(32).fill(3);
                  window.__events=Array.from({length:12},(_,i)=>NostrTools.finalizeEvent(
                    {kind:1,created_at:Math.floor(Date.now()/1000)-i,content:'reply test '+i,tags:[]},key));
                  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);
                  document.querySelector('#btn-nsec-login').click()})()""")
                await b.until("!!__PC.me() && document.querySelectorAll('.note').length>=12")
                await b.js("PCOS.enter()")
                await asyncio.sleep(0.8)

                if open_social_first:
                    await b.js("""(()=>{const i=document.querySelector('.os-icon[data-view=global]');
                                  if(i) i.click();})()""")
                    await asyncio.sleep(1.2)

                # The id of a real post, then ONE request to open it — the click's own endpoint.
                await b.js("window.__target = window.__events[0].id")
                await b.js("__PC.openThread(window.__target)")
                await asyncio.sleep(1.5)
                return await b.js(PROBE)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def _opened(state):
    """Did one call actually put the POST on screen — not merely a frame?"""
    return state['postWindow'] and state['showsPost']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_cold_desktop_opens_a_post_on_the_first_click():
    state = asyncio.run(run(open_social_first=False))
    print('cold=%r' % (state,))
    assert _opened(state), (
        'opening a post from a clean desktop did nothing on the first call: %r' % (state,))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_an_open_social_window_does_not_swallow_the_first_click():
    state = asyncio.run(run(open_social_first=True))
    print('warm=%r' % (state,))
    assert _opened(state), (
        'with Social already open, the first click to open a post did nothing — the user has to '
        'click twice. Same call, same post; the only difference is that a Social window existed '
        'when it ran: %r' % (state,))
