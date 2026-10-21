"""The quoted post under a notification starts at ONE fixed indent, whatever the avatar count.

Reported (git issue "Enhancement: Reorder notifications", phone screenshots): the bordered box quoting
your post sat in the TEXT column, and the text column starts after the avatars -- so a reaction from one
person put the box at one x, three stacked avatars pushed it ~54px further right and narrower, and a
column of notifications read as a ragged staircase of differently sized boxes. The box (and the time
under it) now starts right after the type-icon column, under the avatar, and runs to the right edge.

Drives the SHIPPED client (real template, real notifview.js/client.css) against a stub relay, logs in,
opens Notifications, and measures the rendered boxes at phone and desktop width.
"""
import asyncio
import json
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

# Events are built IN the page (NostrTools signs them) and handed to the stub relay through
# window.__events, which INIT's FixtureSocket answers every REQ from.
SEED = r'''(()=>{
  const sk=n=>new Uint8Array(32).fill(n), now=Math.floor(Date.now()/1000);
  const me=sk(1), mePk=NostrTools.getPublicKey(me), ev=[];
  const sign=(k,t,d)=>NostrTools.finalizeEvent(Object.assign({created_at:now-t,tags:[]},d),k);
  const post=(i)=>{const p=sign(me,5000+i,{kind:1,content:'Quoted post number '+i+' -- long enough to wrap onto a second line on a phone screen'});ev.push(p);return p;};
  for(let n=2;n<=12;n++) ev.push(sign(sk(n),9000,{kind:0,content:JSON.stringify({name:'person'+n})}));
  const react=(p,who,t)=>who.forEach((n,j)=>ev.push(sign(sk(n),t+j,{kind:7,content:'+',tags:[['e',p.id],['p',mePk]]})));
  const p1=post(1),p2=post(2),p3=post(3),p4=post(4),p5=post(5);
  ev.push(sign(sk(2),100,{kind:1,content:'a reply to your post',tags:[['e',p1.id,'','root'],['p',mePk]]}));
  react(p2,[3],200);                   // one actor: a single avatar
  react(p3,[4,5,6],300);               // three actors: a 3-avatar stack
  react(p4,[7,8,9,10,11,12],400);      // six actors: a row of six faces
  ev.push(sign(sk(5),500,{kind:6,content:'',tags:[['e',p5.id],['p',mePk]]}));   // a repost
  window.__events=ev;
  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(me);
  document.querySelector('#btn-nsec-login').click();
})()'''

MEASURE = r'''(()=>{
  return [...document.querySelectorAll('#feed .notif[data-open]')].map(row=>{
    const box=row.querySelector('.notif-ctx .quoted'), ts=row.querySelector('.notif-ts')||[...row.querySelectorAll('.muted.small')].pop();
    const r=box&&box.getBoundingClientRect(), av=row.querySelector('.notif-av').getBoundingClientRect();
    return {avatars:row.querySelectorAll('.notif-av').length, text:row.textContent.slice(0,60),
      left:r?r.left:null, right:r?r.right:null, avLeft:av.left, tsLeft:ts?ts.getBoundingClientRect().left:null,
      vw:document.documentElement.clientWidth};
  });
})()'''


async def run(width, shot=None):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-notif-indent-', ignore_cleanup_errors=True) as profile:
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
                await b.call('Network.enable')
                await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',
                             {'width': width, 'height': 1400, 'deviceScaleFactor': 1, 'mobile': width < 600})
                await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': INIT})
                await b.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/client'})
                await b.until("document.body?.classList.contains('guest')")
                await b.js(SEED)
                await b.until('!!window.__PC?.me()')
                await b.js("__PC.switchView('notifications')")
                # Every row's quoted post has landed (not the "…" placeholder) and the stacks are drawn.
                await b.until("""(()=>{const rows=[...document.querySelectorAll('#feed .notif[data-open]')];
                  return rows.length>=5 && rows.every(r=>r.querySelector('.notif-ctx .quoted'))
                    && rows.some(r=>r.querySelectorAll('.notif-av').length===3)
                    && rows.some(r=>r.querySelectorAll('.notif-rxrow .notif-av').length===6);})()""")
                await b.js('document.fonts.ready.then(()=>new Promise(r=>requestAnimationFrame(()=>r(true))))')
                rows = await b.js(MEASURE)
                if shot:
                    data = await b.call('Page.captureScreenshot', {'format': 'png'})
                    import base64
                    Path(shot).write_bytes(base64.b64decode(data['data']))
                return rows
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
            server.server_close()


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [412, 1280])
def test_quoted_post_box_starts_at_the_same_x_for_every_notification(width):
    rows = asyncio.run(run(width))
    counts = sorted({r['avatars'] for r in rows})
    assert 1 in counts and 3 in counts, rows          # the fixture really has single and stacked rows
    lefts = [r['left'] for r in rows]
    assert max(lefts) - min(lefts) <= 1, f'quoted box moves with the avatar count: {json.dumps(rows, indent=1)}'
    for r in rows:
        # Starts under the avatar column, never in the text column beside it ...
        assert abs(r['left'] - r['avLeft']) <= 1, r
        # ... and still fits the screen.
        assert r['right'] <= r['vw'] + 0.5, r
        # The time under the box lines up with the box.
        assert r['tsLeft'] is not None and abs(r['tsLeft'] - r['left']) <= 1, r


if __name__ == '__main__':  # python -m tests.client.<this> <width> <out.png>  -- capture a screenshot
    import sys
    print(json.dumps(asyncio.run(run(int(sys.argv[1]), sys.argv[2])), indent=1))
