"""WHEN PBS CANNOT GO LIVE IT SAYS WHY -- AND HAS PUBLISHED NOTHING.

Run: venv-unified/bin/python -m pytest tests/client/test_pbs_errors_full_app.py

Each failure is driven through the real Go Live button. For every one: the status bar names the cause in words a
person can act on, the pill reads NOT LIVE, and no kind-30311 (and no announcement post) was signed -- a stream
announced over a publish that never happened is a dead link on every client that saw it. The thumbnail is uploaded
only after the media server took the publish, so none of these uploads anything either.

  no instance        a desktop/APK bundle with no instance: Go Live is disabled and says streaming needs one
  token refused      the instance says this account may not stream -> its own message, no WHIP attempted
  streaming off      the instance has streaming turned off
  unreachable        the ingest request never reaches the instance
  media server down  ingest answers server_up:false
  WHIP refused       the media server answers 502 -> "isn't answering", nothing announced
  macOS permission   the desktop app reports Screen Recording denied -> PBS says where to turn it on, and its
                     button opens that settings pane
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CASES = {
    'refused': ({'__ingestMode': 'refused'}, 'enabled for your account', False),
    'off': ({'__ingestMode': 'off'}, 'turned off', False),
    'down': ({'__ingestMode': 'down'}, 'couldn’t reach your instance', False),
    'mediaserver_down': ({'__ingestMode': 'mediaserver_down'}, 'stream server isn’t answering', False),
    'whip_fail': ({'__whipMode': 'fail'}, 'stream server isn’t answering', True),
}


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('case', sorted(CASES))
def test_a_refused_go_live_says_why_and_publishes_nothing(case):
    flags, words, whip_tried = CASES[case]
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        await F.click(b, '#pbs-add-text')
        await F.click(b, '#pbs-go')
        await b.until("document.querySelector('#pbs-status').dataset.state==='error'")
        await asyncio.sleep(1)
        got['why'] = await b.js("document.querySelector('#pbs-why').textContent")
        got['kind'] = await b.js("document.querySelector('#pbs-why').dataset.kind")
        got['pill'] = await b.js("document.querySelector('#pbs-status').textContent.trim()")
        got['live'] = await b.js("__published.filter(e=>e.kind===30311||(e.kind===1&&e.tags.some(t=>t[0]==='t'&&t[1]==='livestream'))).length")
        got['whip'] = await b.js('__whip.length + (__whipMode==="fail"?1:0)')
        got['button'] = await b.js("({t:document.querySelector('#pbs-go').textContent.trim(),d:document.querySelector('#pbs-go').disabled})")
    asyncio.run(desktop.with_browser('online', '', check, F.init(**flags)))
    assert words in got['why'], (case, got['why'])
    assert got['kind'] == 'error' and got['pill'] == 'NOT LIVE', got
    assert got['live'] == 0, ('a refused go-live published a stream event', case, got['live'])
    if not whip_tried:
        assert got['whip'] == 0, ('nothing should have been sent to the media server', case)
    assert got['button'] == {'t': 'Go Live', 'd': False}, ('the button must come back for a retry', got['button'])


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_with_no_instance_go_live_says_streaming_needs_one():
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        got['note'] = await b.js("(document.querySelector('#pbs-noinst')||{}).textContent||''")
        got['disabled'] = await b.js("document.querySelector('#pbs-go').disabled")
        await b.js("document.querySelector('#pbs-go').disabled=false;document.querySelector('#pbs-go').click();true")
        await asyncio.sleep(1)
        got['why'] = await b.js("document.querySelector('#pbs-why').textContent")
        got['ingest'] = await b.js('__ingestCalls')
        got['live'] = await b.js("__published.filter(e=>e.kind===30311).length")
        # composing still works with no instance
        await F.click(b, '#pbs-add-text')
        await b.until("document.querySelectorAll('.pbs-layer').length===1")
        await asyncio.sleep(.4)                            # the next frame of the compositor clock
        got['text'] = await F.count(b, (60, 232, 255), None, 40)
    asyncio.run(desktop.with_browser('online', '', check, F.init() + "window.pcShell.instanceSync='';"))
    assert 'needs a PosterChan instance' in got['note'] and got['disabled'], got
    assert 'needs a PosterChan instance' in got['why'] and got['ingest'] == 0 and got['live'] == 0, got
    assert got['text']['n'] > 500, 'PBS must still compose with no instance'


MAC = r"""window.__macOpened=0;window.pcCapture={info:async()=>({platform:'darwin',screen:'denied',features:[]}),
  openSettings:async()=>{window.__macOpened++;return true}};"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_denied_macos_screen_permission_is_explained_with_the_way_to_fix_it():
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        await F.click(b, '#pbs-add-screen')
        await b.until("!document.querySelector('#pbs-mac').hidden")
        got['why'] = await b.js("document.querySelector('#pbs-why').textContent")
        got['gdm'] = await b.js('__gdm')
        await F.click(b, '#pbs-mac-open')
        await asyncio.sleep(.3)
        got['opened'] = await b.js('__macOpened')
        got['layers'] = await b.js("document.querySelectorAll('.pbs-layer').length")
    asyncio.run(desktop.with_browser('online', '', check, F.init() + MAC))
    assert 'Screen Recording' in got['why'] and 'Privacy & Security' in got['why'], got['why']
    assert got['gdm'] == 0, 'no capture should be attempted while macOS refuses it'
    assert got['opened'] == 1, 'the button must open the Screen Recording settings'
    assert got['layers'] == 0, 'a capture that never started must not leave a layer'
