"""ENTER AFTER SENDING A DM DOES NOT CLOSE THE CONVERSATION.

Reported twice (2026-10-28): "I pressed enter after sending DM and the convo closed". Real key events (CDP
Input.dispatchKeyEvent, which go wherever focus actually is -- a synthetic keydown on #dm-in would not notice focus
having moved): type a message, Enter to send, Enter again. The conversation must still be open and the next message
must go into the box, on the web page and in a PosterChanOS Messages window, with a fast and a slow relay.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY
from tests.client.test_dm_send_scrolls_to_bottom_full_app import SEED


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _enter(b):
    for kind in ('keyDown', 'keyUp'):
        await b.call('Input.dispatchKeyEvent', {'type': kind, 'key': 'Enter', 'code': 'Enter',
                                                'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13,
                                                **({'text': '\r'} if kind == 'keyDown' else {})})


STATE = r'''(pk=>({open:!!document.querySelector('#dm-in')&&!!document.querySelector('#dm-msgs'),
  peer:(document.querySelector('[data-dm-peer],#dm-msgs')||{}).dataset?.dmPeer||'',
  focus:(document.activeElement&&(document.activeElement.id||document.activeElement.tagName))||'',
  view:(window.__PC&&__PC.view&&__PC.view())||''}))'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('desk', ['web', 'posterchanos', 'desktop-mode', 'phone'])
@pytest.mark.parametrize('relay_ms', [60, 1500])
@pytest.mark.parametrize('how', ['enter', 'button'])
def test_enter_after_sending_keeps_the_conversation_open(desk, relay_ms, how):
    got = {}

    async def check(b):
        await b.js("localStorage.setItem('__relayMs','" + str(relay_ms) + "')")
        if desk == 'phone':
            await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        elif desk == 'desktop-mode':   # the tablet: 1408 css px, the in-page windowed desktop
            await b.call('Emulation.setDeviceMetricsOverride', {'width': 1408, 'height': 880, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        pk = await b.js(SEED + '(5)')
        if desk == 'desktop-mode':
            await b.js("PCOS.enter();true")
            await b.until("document.body.classList.contains('os-on')")
            await b.js("PCOS.routeView('messages');true")
            # The desktop buddy stands over the Messages window's Send button at 1408 px; she is not what this tests.
            await b.js("(()=>{const st=document.createElement('style');st.textContent='.os-buddy{display:none!important}';document.head.appendChild(st);return true})()")
        else:
            await b.js("__PC.switchView('messages')")
        row = "#dm-rows .dm-peer[data-peer=" + json.dumps(pk) + "]"
        await b.until("!!document.querySelector(" + json.dumps(row) + ")")
        await b.js("document.querySelector(" + json.dumps(row) + ").click()")
        await b.until("!!document.querySelector('#dm-in') && document.querySelectorAll('#dm-msgs .bubble').length>=3")
        await asyncio.sleep(1)
        await b.js("document.querySelector('#dm-in').focus()")
        await b.call('Input.insertText', {'text': 'first message'})
        if how == 'enter':
            await _enter(b)
        else:   # a real click on Send: focus goes to the button, which is then disabled while it sends
            # Where the browser itself draws it (desktop mode zooms the page, so getBoundingClientRect is not screen px).
            doc = await b.call('DOM.getDocument', {'depth': 0})
            node = await b.call('DOM.querySelector', {'nodeId': doc['root']['nodeId'], 'selector': '#dm-send'})
            q = (await b.call('DOM.getContentQuads', {'nodeId': node['nodeId']}))['quads'][0]
            r = [(q[0] + q[4]) / 2, (q[1] + q[5]) / 2]
            await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': r[0], 'y': r[1]})   # a mouse arrives first
            for kind in ('mousePressed', 'mouseReleased'):
                await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': r[0], 'y': r[1], 'button': 'left', 'clickCount': 1})
        await b.until("[...document.querySelectorAll('#dm-msgs .bubble')].some(x=>x.textContent.includes('first message'))")
        await asyncio.sleep(1.5 + relay_ms / 1000 * 3)
        got['before'] = await b.js(STATE + '()')
        await _enter(b)                                   # the Enter that closed it
        await asyncio.sleep(1.5)
        got['after'] = await b.js(STATE + '()')
        await b.call('Input.insertText', {'text': 'second message'})
        got['typed'] = await b.js("(document.querySelector('#dm-in')||{}).value||''")

    extra = RELAY
    if desk == 'posterchanos':
        extra += "window.pcShell.windowContext={role:'app',view:'messages'};window.pcShell.backgroundOwner=false;"
    asyncio.run(desktop.with_browser('online', '?pcwin=messages' if desk == 'posterchanos' else '', check, extra))
    assert got['after']['open'], ('Enter after sending closed the conversation', got)
    assert got['before']['focus'] == 'dm-in', ('after sending, focus left the message box', got)
    assert 'second message' in got['typed'], ('the next message did not go into the box', got)
