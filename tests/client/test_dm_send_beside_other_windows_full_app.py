"""SENDING A DM IN DESKTOP MODE, WITH OTHER WINDOWS OPEN, KEEPS THE CONVERSATION OPEN.

Reported 2026-10-29 on the Windows app: "I send message and the convo goes away still", "after clicking send or enter",
"convo is in list but goes back to that default screen with no convo open". The 10-28 fix (focus after Send) was tested
with Messages ALONE on the desktop; the Windows app opens Messages beside other windows. Real key and mouse events,
the shipped bundle, the in-page windowed desktop.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY
from tests.client.test_dm_send_scrolls_to_bottom_full_app import SEED
from tests.client.test_dm_enter_after_send_keeps_the_conversation_full_app import STATE, _enter


# LIVE DELIVERY: the stub relay answers REQs from storage but never pushes; this keeps every open subscription and
# hands a new event to the ones whose filter it matches, as a relay does.
LIVE = r"""
(()=>{const P=RelaySocket.prototype,send=P.send;window.__subs=[];
 P.send=function(raw){const m=JSON.parse(raw);if(Array.isArray(m)&&m[0]==='REQ')__subs.push({s:this,id:m[1],fs:m.slice(2)});
   if(Array.isArray(m)&&m[0]==='CLOSE')__subs=__subs.filter(x=>!(x.s===this&&x.id===m[1]));return send.call(this,raw);};
 window.__deliver=ev=>{const r=_rel();r.push(ev);localStorage.setItem('__relayEvents',JSON.stringify(r));let n=0;
   for(const x of __subs)if(x.s.readyState===1&&x.fs.some(f=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f['#p']||ev.tags.some(t=>t[0]==='p'&&f['#p'].includes(t[1])))&&(!f.since||ev.created_at>=f.since))){x.s.fire('message',['EVENT',x.id,ev]);n++;}
   return n;};})();
"""
# A NIP-17 DM from the seeded peer, created NOW (its outer wrap back-dated a little, as NIP-59 does).
# `ahead`: the SENDER's clock is that many seconds ahead of ours -- i.e. this computer's clock is behind, which is how a
# reply typed and sent after their message got dated before it ("my replies are not going in correct order", 10-29).
INCOMING = r"""((text,ahead)=>{const me=new Uint8Array(32).fill(1),mePk=NostrTools.getPublicKey(me),peer=new Uint8Array(32).fill(7),now=Math.floor(Date.now()/1000)+(ahead||0);
 const seal=NostrTools.nip59.createSeal(NostrTools.nip59.createRumor({kind:14,created_at:now,tags:[['p',mePk]],content:text},peer),peer,mePk);
 const eph=NostrTools.generateSecretKey();
 return __deliver(NostrTools.finalizeEvent({kind:1059,created_at:now-60,tags:[['p',mePk]],
   content:NostrTools.nip44.encrypt(JSON.stringify(seal),NostrTools.nip44.getConversationKey(eph,mePk))},eph));})"""


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _click_visible(b, selector):
    """A real click on a part of `selector` that is not covered by another window -- the browser itself says which
    element is on top at each sampled point (DOM.getNodeForLocation), as a person clicks wherever the window shows."""
    doc = await b.call('DOM.getDocument', {'depth': 0})
    node = await b.call('DOM.querySelector', {'nodeId': doc['root']['nodeId'], 'selector': selector})
    assert node.get('nodeId'), selector
    q = (await b.call('DOM.getContentQuads', {'nodeId': node['nodeId']}))['quads'][0]
    xs, ys = sorted([q[0], q[2], q[4], q[6]]), sorted([q[1], q[3], q[5], q[7]])
    for fy in (0.05, 0.3, 0.5, 0.7, 0.95):
        for fx in (0.05, 0.3, 0.5, 0.7, 0.95):
            x, y = xs[0] + (xs[-1] - xs[0]) * fx, ys[0] + (ys[-1] - ys[0]) * fy
            hit = await b.call('DOM.getNodeForLocation', {'x': int(x), 'y': int(y), 'includeUserAgentShadowDOM': False})
            obj = await b.call('DOM.resolveNode', {'backendNodeId': hit['backendNodeId']})
            inside = await b.call('Runtime.callFunctionOn', {'objectId': obj['object']['objectId'], 'returnByValue': True,
                'functionDeclaration': 'function(s){const e=this.nodeType===1?this:this.parentElement;return !!(e&&e.closest(s));}',
                'arguments': [{'value': selector}]})
            if inside['result'].get('value'):
                await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
                for kind in ('mousePressed', 'mouseReleased'):
                    await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
                return
    raise AssertionError('no visible point of ' + selector)


async def _click(b, selector):
    """A real mouse click where the browser draws `selector` (the desktop zooms the page, so not getBoundingClientRect)."""
    doc = await b.call('DOM.getDocument', {'depth': 0})
    node = await b.call('DOM.querySelector', {'nodeId': doc['root']['nodeId'], 'selector': selector})
    assert node.get('nodeId'), selector
    q = (await b.call('DOM.getContentQuads', {'nodeId': node['nodeId']}))['quads'][0]
    x, y = (q[0] + q[4]) / 2, (q[1] + q[5]) / 2
    await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('how', ['enter', 'button'])
@pytest.mark.parametrize('relay_ms', [60, 1500])
@pytest.mark.parametrize('away', [False, True])
def test_sending_beside_other_windows_keeps_the_conversation_open(how, relay_ms, away):
    got = {}

    async def check(b):
        await b.js("localStorage.setItem('__relayMs','" + str(relay_ms) + "')")
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1600, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        pk = await b.js(SEED + '(5)')
        await b.js("PCOS.enter();true")
        await b.until("document.body.classList.contains('os-on')")
        await b.js("(()=>{const st=document.createElement('style');st.textContent='.os-buddy{display:none!important}';document.head.appendChild(st);return true})()")
        # the Windows app's desk: other windows open first, Messages last
        for v in ('notifications', 'social'):
            await b.js("PCOS.routeView(%s);true" % json.dumps(v))
            await asyncio.sleep(0.8)
        await b.js("PCOS.routeView('messages');true")
        row = "#dm-rows .dm-peer[data-peer=" + json.dumps(pk) + "]"
        await b.until("!!document.querySelector(" + json.dumps(row) + ")")
        await b.js("document.querySelector(" + json.dumps(row) + ").click()")
        await b.until("!!document.querySelector('#dm-in') && document.querySelectorAll('#dm-msgs .bubble').length>=3")
        await asyncio.sleep(1)
        if away:   # click ANOTHER window, then click back into the Messages box -- as a person does
            await b.js("[...document.querySelectorAll('.osw')].forEach(w=>{const t=(w.querySelector('.osw-title')||{}).textContent||'';w.dataset.t=t.trim().toLowerCase();});PCOS.arrangeInPage('grid');true"); await asyncio.sleep(1.5)  # the arrangement animates
            await _click(b, ".os-task[data-kind='web'][title='Notifications']")
            await asyncio.sleep(1)
            got['focusedAway'] = await b.js("(document.querySelector('.osw.focused .osw-title')||{}).textContent||''")
            # a DM arrives while Messages is NOT the window in front: its open thread must show it
            # ("I have a DM convo open, I just got 2 notifications for a DM, but DM chat is not updating")
            got['delivered'] = await b.js(INCOMING + "('while you were away', 90)")
            await asyncio.sleep(2.5)
            got['arrivedInThread'] = await b.js("[...document.querySelectorAll('#dm-msgs .bubble')].some(x=>x.textContent.includes('while you were away'))")
            # back to Messages by its title bar (the other window now covers part of it), then into the box
            await _click(b, ".os-task[data-kind='web'][title='Messages']")
            await asyncio.sleep(1)
            got['focusedBack'] = await b.js("(document.querySelector('.osw.focused .osw-title')||{}).textContent||''")
            await _click(b, "#dm-in")
            await asyncio.sleep(0.5)
        else:
            await b.js("document.querySelector('#dm-in').focus()")
        await b.call('Input.insertText', {'text': 'first message'})
        if how == 'enter':
            await _enter(b)
        else:
            doc = await b.call('DOM.getDocument', {'depth': 0})
            node = await b.call('DOM.querySelector', {'nodeId': doc['root']['nodeId'], 'selector': '#dm-send'})
            q = (await b.call('DOM.getContentQuads', {'nodeId': node['nodeId']}))['quads'][0]
            r = [(q[0] + q[4]) / 2, (q[1] + q[5]) / 2]
            await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': r[0], 'y': r[1]})
            for kind in ('mousePressed', 'mouseReleased'):
                await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': r[0], 'y': r[1], 'button': 'left', 'clickCount': 1})
        await b.until("[...document.querySelectorAll('#dm-msgs .bubble')].some(x=>x.textContent.includes('first message'))")
        # the relay's copy of our own message, the notifications/social windows reacting to it, the list refresh
        await asyncio.sleep(3 + relay_ms / 1000 * 3)
        got['after'] = await b.js(STATE + '()')
        got['empty'] = await b.js("(()=>{const t=document.querySelector('#dm-thread');return !!t && /Select a conversation/.test(t.textContent)})()")
        # "my replies are not going in correct order ... its going above new messages that came in while I was typing"
        got['order'] = await b.js("[...document.querySelectorAll('#dm-msgs .bubble')].map(x=>x.textContent.includes('while you were away')?'away':x.textContent.includes('first message')?'mine':'').filter(Boolean)")

    asyncio.run(desktop.with_browser('online', '', check, RELAY + LIVE))
    assert got['after']['open'] and not got['empty'], ('sending closed the conversation (empty pane)', got)
    if away:
        assert got['delivered'] >= 1, ('the stub relay delivered nothing -- the test is not testing', got)
        assert got['arrivedInThread'], ('a DM that arrived while another window was in front never reached the open thread', got)
        assert got['order'] == ['away', 'mine'], ('your reply is not below the message that arrived before it', got)
