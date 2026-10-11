"""REPORT FROM A NOTIFICATION: the ⋯ on a notification card files a NIP-56 report through the ONE sheet.

"add report to notification cards". What the person means depends on the card, and the menu offers
exactly that: a reply/mention is THEIR words, so "Report this post" (['e', reply] + ['p', author]) and the
person; a reaction or repost carries no post of theirs (the quoted post is YOURS), so only the person --
and a grouped reaction row names each of its people. The ⋯ must not open the post (the row's own click),
and must not move the quoted box off its one fixed indent (test_notification_quote_box_fixed_indent).

Drives the SHIPPED client (real template, notifview.js, menus.js, client.css) against the stub relay of
test_effects_full_app; only the relay socket is a fixture, and it records every published EVENT.
"""
import asyncio
import base64
import os
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
SHOTS = Path(os.environ.get('PC_REPORT_SHOTS', '/tmp/pc-report-shots'))

SEED = r'''(()=>{
  const sk=n=>new Uint8Array(32).fill(n), now=Math.floor(Date.now()/1000);
  const me=sk(1), mePk=NostrTools.getPublicKey(me), ev=[];
  const sign=(k,t,d)=>NostrTools.finalizeEvent(Object.assign({created_at:now-t,tags:[]},d),k);
  for(let n=2;n<=6;n++) ev.push(sign(sk(n),9000,{kind:0,content:JSON.stringify({name:'person'+n})}));
  const mine=sign(me,5000,{kind:1,content:'My own post that people answered'}); ev.push(mine);
  const mine2=sign(me,5001,{kind:1,content:'Another post of mine'}); ev.push(mine2);
  window.__reply=sign(sk(2),100,{kind:1,content:'an abusive reply to your post',tags:[['e',mine.id,'','root'],['p',mePk]]});
  ev.push(__reply);
  window.__like=sign(sk(3),200,{kind:7,content:'+',tags:[['e',mine2.id],['p',mePk]]}); ev.push(__like);
  [4,5].forEach((n,j)=>ev.push(sign(sk(n),300+j,{kind:6,content:'',tags:[['e',mine.id],['p',mePk]]})));
  window.__pk=n=>NostrTools.getPublicKey(sk(n));
  window.__events=ev; window.__publishOK=true;
  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(me);
  document.querySelector('#btn-nsec-login').click();
})()'''


async def scenario(width, check):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-notif-report-') as profile:
        proc = subprocess.Popen([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
                                 '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(150):
                if Path(profile, 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, 'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f'http://127.0.0.1:{port}/json')).json()
            url = next(p for p in pages if p.get('type') == 'page')['webSocketDebuggerUrl']
            async with websockets.connect(url, max_size=40_000_000) as ws:
                b = Browser(ws)
                await b.call('Page.enable')
                await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',
                             {'width': width, 'height': 800, 'deviceScaleFactor': 1, 'mobile': width < 600})
                await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': INIT})
                await b.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/client'})
                await b.until("document.body?.classList.contains('guest')")
                await b.js(SEED)
                await b.until('!!window.__PC?.me()')
                await b.js("__PC.switchView('notifications')")
                await b.until("!!document.querySelector('#feed .notif[data-open=\"'+__reply.id+'\"]')")
                await check(b)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
            server.server_close()


def _row(sel_js):
    return "document.querySelector('#feed .notif[data-open=\"'+" + sel_js + "+'\"]')"


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [360, 1280])
def test_report_a_reply_from_its_notification(width):
    async def check(b):
        row = _row('__reply.id')
        await b.until(f"!!{row}.querySelector('.notif-more') && {row}.textContent.includes('person2')")
        view = await b.js("__PC.isView('notifications')")
        await b.js(f"{row}.querySelector('.notif-more').click()")
        await b.until("!!document.querySelector('.menu-pop [data-m]')")
        assert await b.js("__PC.isView('notifications')") == view, "the ⋯ opened the post instead of a menu"
        items = await b.js("[...document.querySelectorAll('.menu-pop [data-m]')].map(x=>[x.dataset.m,x.textContent])")
        assert [m for m, _ in items] == ['post:' + await b.js('__reply.id'), 'user:' + await b.js('__pk(2)')], items
        assert 'person2' in items[1][1], items
        await b.js("document.querySelector('.menu-pop [data-m^=\"post:\"]').click()")
        await b.until("!!document.querySelector('.rep-form')")
        geo = await b.js("""(()=>{const m=document.querySelector('.rep-form').closest('.modal').getBoundingClientRect(),
            s=document.querySelector('#rep-submit').getBoundingClientRect();
            return {l:m.left,r:m.right,sb:s.bottom,vh:innerHeight,vw:document.documentElement.clientWidth,
                    page:document.documentElement.scrollWidth}})()""")
        assert geo['l'] >= 0 and geo['r'] <= geo['vw'] + .5 and geo['page'] <= geo['vw'] and geo['sb'] <= geo['vh'], geo
        if width == 360:
            SHOTS.mkdir(parents=True, exist_ok=True)
            (SHOTS / 'notif-report-sheet-360.png').write_bytes(
                base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))
        await b.js("""(()=>{document.querySelector('.rep-form input[value=profanity]').click();
            document.querySelector('#rep-comment').value='Abusive reply';document.querySelector('#rep-submit').click()})()""")
        await b.until("__published.some(e=>e.kind===1984)")
        rep = await b.js("__published.find(e=>e.kind===1984)")
        nip56 = [t for t in rep['tags'] if t[0] in ('e', 'p', 'L', 'l', 'x')]
        assert nip56 == [['e', await b.js('__reply.id'), 'profanity'], ['p', await b.js('__pk(2)'), 'profanity']], rep['tags']
        assert rep['content'] == 'Abusive reply'
    asyncio.run(scenario(width, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_a_reaction_or_a_repost_reports_the_person_never_your_own_post():
    async def check(b):
        await b.until("[...document.querySelectorAll('#feed .notif .notif-more')].length>=3")
        # The single reaction: only its person (the quoted post is MINE -- reporting it means nothing).
        like_row = _row("__like.tags[0][1]")
        await b.js(f"{like_row}.querySelector('.notif-more').click()")
        await b.until("!!document.querySelector('.menu-pop [data-m]')")
        assert await b.js("[...document.querySelectorAll('.menu-pop [data-m]')].map(x=>x.dataset.m)") == ['user:' + await b.js('__pk(3)')]
        await b.js("document.body.click()")
        await asyncio.sleep(.2)
        # The grouped repost row (two people): one entry per person.
        rt = await b.js("""(()=>{const r=document.querySelector('#feed .notif.rt');r.querySelector('.notif-more').click();return true})()""")
        assert rt
        await b.until("document.querySelectorAll('.menu-pop [data-m]').length===2")
        got = sorted(await b.js("[...document.querySelectorAll('.menu-pop [data-m]')].map(x=>x.dataset.m)"))
        assert got == sorted(['user:' + await b.js('__pk(4)'), 'user:' + await b.js('__pk(5)')]), got
        await b.js("document.querySelector('.menu-pop [data-m]').click()")
        await b.until("!!document.querySelector('.rep-form')")
        await b.js("""(()=>{document.querySelector('.rep-form input[value=spam]').click();document.querySelector('#rep-submit').click()})()""")
        await b.until("__published.some(e=>e.kind===1984)")
        rep = await b.js("__published.find(e=>e.kind===1984)")
        assert not [t for t in rep['tags'] if t[0] == 'e'], ('a person report named a post', rep['tags'])
        assert [t[0] for t in rep['tags'] if t[0] == 'p'] == ['p'], rep['tags']
    asyncio.run(scenario(1280, check))
