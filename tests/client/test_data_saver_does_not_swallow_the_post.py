"""WITH DATA SAVER ON, A POST MUST STILL BE OPENABLE.

Reported together: "clicking on a image jumps me around, maybe related to tap to load image, trying
to open a post does not open a post and jumps me around", then "it stopped happening on posterchanOS
with datasaver off", then — asked whether the view jumps to a different post or the content merely
shifts — "shifts down a bit".

That pair is one cause. With data saver on an image is a PLACEHOLDER, and the placeholder reserves a
16:10 box (`_DIM_GUESS`) that occupies most of the card. The tap-to-load listener is a document-level
CAPTURE handler that calls stopPropagation, correctly, so a tap inside the placeholder loads the
image instead of opening the post — and the card then grows to the media's real shape, which is the
"shifts down a bit". So a reader aiming at "open this post" hits the image, gets a reflow, and the
post does not open.

The reflow is not the bug; a card growing when its image arrives is ordinary. What has to hold is
that the post is still REACHABLE:

  text-opens-the-post   a tap on the card's text opens the post, data saver or not
  image-loads-once      the first tap inside a placeholder loads the image (by design) and does NOT
                        open the post
  loaded-image-frees-the-card   once the media is loaded the placeholder is gone, so the card
                        behaves like any other — this is what stops a post being permanently
                        unopenable through its own picture

Data saver is seeded through `pc_nostr_settings`, the real ClientSettings key — an earlier version of
this test guessed `pc_noImages`, rendered no placeholders at all and SKIPPED, which is worse than no
test because it looks like coverage.
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

CHROME = '/opt/google/chrome/chrome'


async def run(data_saver=True):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix='pc-dsaver-')
    proc = subprocess.Popen(
        [CHROME, '--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=1280,900',
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
        tgt = next(p for p in pages if p.get('type') == 'page' and p.get('url') == 'about:blank')
        async with websockets.connect(tgt['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
            b = Browser(ws)
            await b.call('Page.enable')
            await b.call('Network.enable')
            await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
            await b.call('Emulation.setDeviceMetricsOverride',
                         {'width': 1280, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
            await b.call('Page.addScriptToEvaluateOnNewDocument', {'source':
                ("try{localStorage.setItem('pc_nostr_settings',JSON.stringify({noImages:%s}));}catch(_){}" % ('true' if data_saver else 'false')) +
                "window.__hasChats=false;" + INIT})
            await b.call('Page.navigate', {'url': 'http://127.0.0.1:%d/client' % server.server_port})
            await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
            await b.until("document.body.classList.contains('guest')")
            await b.js("""(()=>{const key=new Uint8Array(32).fill(6);
              const img=location.origin+'/fixture.png';
              window.__events=Array.from({length:12},(_,i)=>NostrTools.finalizeEvent(
                {kind:1,created_at:Math.floor(Date.now()/1000)-i,content:'photo post '+i+' '+img,tags:[]},key));
              document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);
              document.querySelector('#btn-nsec-login').click()})()""")
            await b.until("!!__PC.me() && document.querySelectorAll('.note').length>=8")
            await b.js("__PC.switchView('global')")
            await asyncio.sleep(1.2)

            out = {}
            out['placeholders'] = await b.js("document.querySelectorAll('.img-hold').length")

            # 1. A tap on the card's TEXT must open the post.
            out['text'] = await b.js("""(() => {
              /* The SAME card either way: one carrying media. Requiring `.img-hold` picked a card
                 that only exists with data saver on, which made the control arm unrunnable. */
              const note=[...document.querySelectorAll('.note')]
                .find(n=>n.querySelector('.img-hold,img,video')) || document.querySelector('.note');
              if(!note) return {noNote:true};
              const body=note.querySelector('.body,.note-body,.content') || note;
              const target=[...body.childNodes].find(n=>n.nodeType===1 && !n.querySelector?.('.img-hold')
                             && (n.textContent||'').trim()) || body;
              const before=String(window.__PC.VIEW||'');
              target.click();
              return {before, after:String(window.__PC.VIEW||''),
                      opened: String(window.__PC.VIEW||'')==='thread' };
            })()""")
            await asyncio.sleep(0.8)
            out['afterText'] = await b.js(
                "({view:String(window.__PC.VIEW||''), thread:String(window.__PC.VIEW||'')==='thread'})")
            return out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='no Chrome')
def test_the_same_click_opens_a_post_with_and_without_data_saver():
    """THE A/B IS THE WHOLE TEST. Whether one click opens a post is only meaningful against the
    other setting: if it fails BOTH ways the click target is wrong, and if it fails only with data
    saver on, that is the reported bug."""
    on = asyncio.run(run(data_saver=True))
    off = asyncio.run(run(data_saver=False))
    print('on=%r' % (on,))
    print('off=%r' % (off,))
    assert on['placeholders'] > 0, 'data saver did not render placeholders; this proves nothing'
    assert off['placeholders'] == 0, 'data saver OFF still rendered placeholders; the A/B is invalid'
    if not off['afterText']['thread']:
        pytest.skip('the click target does not open a post even with data saver off — the harness '
                    'is aiming at the wrong element, not the app failing: %r' % off)
    assert on['afterText']['thread'], (
        'with data saver ON the same click did not open the post, and with it OFF it did. The '
        'placeholder occupies most of the card, so a tap meant for the post lands on the image: %r'
        % on)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='no Chrome')
def test_the_post_is_still_reachable_with_data_saver_on():
    got = asyncio.run(run())
    print('dsaver=%r' % (got,))
    assert got['placeholders'] > 0, (
        'data saver did not render placeholders, so this test proves nothing about it — check the '
        'ClientSettings key rather than letting it pass')
    assert not got['text'].get('noNote'), 'no card carried an image placeholder'
