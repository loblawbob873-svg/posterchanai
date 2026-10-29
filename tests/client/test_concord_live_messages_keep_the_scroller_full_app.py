"""A message arriving while somebody scrolls a Communities room does not blank or rebuild the room.

Reported as "Smoother Community Scrolling" (Vyram, APK video; the owner reproduces it in the PosterChanOS
desktop app): scrolling a busy room, the message pane went BLACK -- whole viewport, a second or two at a
time -- and scrolling felt clunky. Measured: every live tick that reached `render()` while the composer
was not focused (a message, a reaction, a profile landing) rebuilt the whole workspace with
`feed.innerHTML`, so the scroller itself and 260 of 260 rows were NEW elements each time, at a 70-90 ms
long task apiece on this machine (150-340 ms on the emulator at phone density). A new scroller is a new
compositor layer with no raster; while input is scrolling, Chromium shows it before its tiles exist, so
the pane is blank until raster catches up -- and on the emulator the rebuild also killed a running fling
outright (563 px travelled instead of ~2,400). The fix patches the pane in place when nothing outside it
changed (the path a live tick already took while somebody was typing).

This drives the SHIPPED client at a phone's size and density with REAL wheel input, delivers a message
every 200 ms through `PCConcord.backgroundRender()` (what a relay event does), and reads the screencast:
before the fix, 4 of 4 runs showed blank frames (14-21% of the pane, only on frames after a rebuild;
0 with no live messages); after it, 0 of 4. The structural checks say why: the scroller and the rows on
screen must be the same nodes after the messages arrived.
"""
import asyncio
import base64
import io
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_effects_full_app import Browser


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
         "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

# A local community: one channel holding 260 messages long enough to wrap, entered through its channel
# button the way a phone enters it.
ROOM = r'''(()=>{window.__sp='pending';const pk=__PC.me().pubkey,ID='live-scroll-room';
localStorage.setItem('pc.concord.rooms.v1.'+pk,JSON.stringify([{name:'Live scroll',naddr:ID,local:true,channels:[{name:'general',private:false}]}]));
localStorage.setItem('pc.concord.active.v1.'+pk,'0');
const words='the relay answered and the room kept every message we sent over the long weekend while somebody fixed the build again'.split(' ');
const msgs=[],t0=Date.now()-300*60000;
for(let i=0;i<260;i++){const n=8+(i*7)%40,s=[];for(let j=0;j<n;j++)s.push(words[(i*3+j)%words.length]);
  msgs.push({id:'m'+i,by:'Member '+(i%9),pubkey:String(i%9).repeat(64),at:t0+i*60000,text:'#'+i+' '+s.join(' ')});}
localStorage.setItem('pc.concord.test.'+ID,JSON.stringify(msgs));
window.__box=()=>[...document.querySelectorAll('.cc-messages')].find(b=>{const r=b.getBoundingClientRect();return r.width>1&&r.height>1;});
__PC.switchView('concord');
let tries=0;const go=()=>{const box=__box(),rows=box?box.querySelectorAll('.cc-message').length:0;
  if(!box||rows<200){if(!box){const ch=document.querySelector('[data-cc-channel="general"]');if(ch)ch.click();}
    if(tries++<100){setTimeout(go,150);return;}window.__sp='error rows='+rows;return;}
  window.__sp='ready';};go();
let n=0;window.__live=()=>{const K='pc.concord.test.'+ID,a=JSON.parse(localStorage.getItem(K)||'[]');
  a.push({id:'live'+n,by:'Member '+(n%9),pubkey:String(n%9).repeat(64),at:Date.now(),text:'live message '+n++});
  localStorage.setItem(K,JSON.stringify(a));PCConcord.backgroundRender();};
return true;})()'''

# What happened to the scroller and the rows while the wheel turned.
WATCH = r'''(()=>{const b=__box();window.__w={box:b,rows:[...b.querySelectorAll('.cc-message')],top:b.scrollTop,jump:0,on:true};
  const t=()=>{const W=window.__w;if(!W.on)return;const nb=__box(),top=nb?nb.scrollTop:0;W.jump=Math.max(W.jump,Math.abs(top-W.top));W.top=top;requestAnimationFrame(t);};
  requestAnimationFrame(t);return true;})()'''
REPORT = r'''(()=>{const W=window.__w;W.on=false;const b=__box();
  return {scrollerReplaced:b!==W.box,rowsReplaced:W.rows.filter(r=>!r.isConnected).length,rows:W.rows.length,
    maxJump:Math.round(W.jump)};})()'''

WIDTH, HEIGHT, DPR = 360, 720, 3
WHEEL_STEP = 200   # px per wheel event; a frame-to-frame scroll bigger than a few of these is a jump


def blank_fraction(png, pane):
    """Share of the pane's pixel rows that sit in a flat run taller than any gap a painted room has."""
    from PIL import Image
    img = Image.open(io.BytesIO(base64.b64decode(png))).convert('RGB')
    k = img.size[0] / WIDTH
    x0, y0 = int(pane[0] * k) + 2, int(pane[1] * k) + 2
    x1, y1 = int((pane[0] + pane[2]) * k) - 2, int((pane[1] + pane[3]) * k) - 2
    band, px, out, run = int(48 * k), img.load(), 0, 0
    for y in range(y0, y1 + 1):
        flat = False
        if y < y1:
            c0 = px[x0, y]
            flat = all(abs(px[x, y][i] - c0[i]) <= 14 for x in range(x0 + 1, x1, 2) for i in range(3))
        if flat:
            run += 1
            continue
        if run >= band:
            out += run
        run = 0
    return out / max(1, y1 - y0)


class FrameBrowser(Browser):
    """The harness's CDP client, also collecting screencast frames that arrive between replies."""
    frames = []

    async def call(self, method, params=None):
        self.sequence += 1
        seq = self.sequence
        await self.ws.send(json.dumps({'id': seq, 'method': method, 'params': params or {}}))
        while True:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), 60))
            if m.get('method') == 'Page.screencastFrame':
                FrameBrowser.frames.append(m['params']['data'])
                self.sequence += 1
                await self.ws.send(json.dumps({'id': self.sequence, 'method': 'Page.screencastFrameAck',
                                               'params': {'sessionId': m['params']['sessionId']}}))
                continue
            if m.get('id') == seq:
                assert 'error' not in m, m
                return m.get('result', {})


async def scroll_while_messages_arrive(b, live_ms):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=WIDTH, height=HEIGHT, deviceScaleFactor=DPR, mobile=True))
    await desktop.login(b)
    await b.js(ROOM)
    await b.until("String(window.__sp).startsWith('ready')||String(window.__sp).startsWith('error')")
    assert await b.js('window.__sp') == 'ready', await b.js('window.__sp')
    await asyncio.sleep(2.5)   # the room's own entry passes (pin to newest) finish
    pane = await b.js("(()=>{const b=__box();b.scrollTop=b.scrollHeight;const r=b.getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
    await asyncio.sleep(1.2)
    await b.call('Page.startScreencast', {'format': 'png', 'everyNthFrame': 1})
    await asyncio.sleep(.4)
    FrameBrowser.frames.clear()
    top0 = await b.js('__box().scrollTop')
    await b.js(WATCH)
    if live_ms:
        await b.js('window.__iv=setInterval(()=>window.__live(),%d);1' % live_ms)
    x, y = pane[0] + pane[2] / 2, pane[1] + pane[3] / 2
    for _ in range(60):   # ~1.5 s of a wheel turned toward older messages
        await b.call('Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': x, 'y': y, 'deltaX': 0, 'deltaY': -WHEEL_STEP})
        await asyncio.sleep(.025)
    await asyncio.sleep(.6)
    if live_ms:
        await b.js('clearInterval(window.__iv);1')
    out = await b.js(REPORT)
    await b.call('Page.stopScreencast')
    out['travelled'] = round(top0 - await b.js('__box().scrollTop'))
    out['live'] = await b.js("JSON.parse(localStorage.getItem('pc.concord.test.live-scroll-room')).length-260")
    out['blank'] = [round(blank_fraction(f, pane) * 100) for f in FrameBrowser.frames]
    return out


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_messages_arriving_during_a_scroll_do_not_blank_or_rebuild_the_room(monkeypatch):
    monkeypatch.setattr(desktop, 'Browser', FrameBrowser)
    got = {}

    async def check(b):
        got['r'] = await scroll_while_messages_arrive(b, 200)
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    r = got['r']
    report = json.dumps({k: v for k, v in r.items() if k != 'blank'}) + '\nblank % per frame: ' + ' '.join(map(str, r['blank']))
    assert r['live'] >= 5, 'messages did not arrive during the scroll, so this proves nothing:\n' + report
    assert r['travelled'] > 3000, 'the wheel did not scroll the room:\n' + report
    assert len(r['blank']) >= 20, 'too few screencast frames to judge:\n' + report
    assert not r['scrollerReplaced'], 'a live message replaced the scroller under the reader:\n' + report
    assert r['rowsReplaced'] == 0, 'a live message rebuilt rows that did not change:\n' + report
    assert r['maxJump'] <= 3 * WHEEL_STEP, 'the room jumped between two frames:\n' + report
    blank = [v for v in r['blank'] if v > 5]
    assert not blank, 'the message pane showed blank areas while messages arrived:\n' + report
