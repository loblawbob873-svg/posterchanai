"""A NOTIFICATION (or a reply) OPENS THE WHOLE CONVERSATION, THREADED, SIGNED IN.

Owner: "replies in a post is broken, if I load a post or reply, I expect to see every post and reply in a
threaded view" and "clicking on a notification should show the post with all replies in a nice threaded
view". Logged out, against the live node, a thread opened from any of its posts drew every card; the
broken path is the SIGNED-IN one.

Drives the SHIPPED client (real template, app.js, notifview.js, client.css), signed in with a real key,
against a stub relay socket that answers REQs the way a relay does -- kinds/ids/authors/#e/#p/since/until/
limit are all honoured, so a `#e` query returns only what actually tags those ids. The conversation:

    ROOT (mine)
     ├─ R1 (alice)                 root-tagged
     │   └─ R2 (bob)               root + reply R1
     │       └─ R3 (carol)  ← the notified, deep reply
     └─ R4 (dave)                  root-tagged
         └─ R5 (erin)              tags ONLY its parent R4 (no root marker; many clients do this)

Every reply p-tags me, so each is a notification. Clicking R3's must show ROOT and R1..R5, each nested
under its real parent, R3 highlighted and scrolled into view. Covered: the notified event held in the
Store (the cache-first path), the same event evicted from the Store first, and a reply opened from the
timeline.
"""
import asyncio
import os
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, FIRST_RUN_DONE

CHROME = '/opt/google/chrome/chrome'

# The stub relay. Unlike test_effects_full_app's socket (which ignores tag filters and returns every
# event of a kind for any `#e` query -- so a thread test there cannot tell a real expansion from a
# lucky dump), this one matches filters like a relay does.
INIT = FIRST_RUN_DONE + r'''
window.__errors=[];onerror=(m)=>__errors.push(String(m));
window.__published=[];window.__reqs=[];
const origFetch=window.fetch.bind(window);
window.fetch=async function(url,opts={}){
 const u=String(url);
 if(new URL(u,location.href).origin===location.origin && u.includes('/static/'))return origFetch(url,opts);
 let d={};
 if(u.includes('/client/config'))d={nostr_only:false,relay_url:'wss://fixture.invalid',relays:['wss://fixture.invalid']};
 if(u.includes('/auth/nostr-login'))d={access_token:'fixture-token',user:{id:1,can_ai:true,can_blossom:true,is_admin:false}};
 return new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}});
};
const _match=(f,ev)=>{
 if(f.kinds&&!f.kinds.includes(ev.kind))return false;
 if(f.ids&&!f.ids.includes(ev.id))return false;
 if(f.authors&&!f.authors.includes(ev.pubkey))return false;
 if(f.since&&ev.created_at<f.since)return false;
 if(f.until&&ev.created_at>f.until)return false;
 for(const k of Object.keys(f)){ if(k[0]!=='#')continue; const n=k.slice(1);
   if(!(ev.tags||[]).some(t=>t[0]===n&&f[k].includes(t[1])))return false; }
 if(f.search)return false;
 return true;
};
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;setTimeout(()=>{this.readyState=1;this.fire('open',{});},10)}
 fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e)}
 send(raw){const m=JSON.parse(raw);if(!Array.isArray(m))return;
  if(m[0]==='REQ'){__reqs.push(m.slice(2));setTimeout(()=>{const sent=new Set();for(const f of m.slice(2)){let n=0;
    const hits=(window.__events||[]).filter(ev=>_match(f,ev)).sort((a,b)=>b.created_at-a.created_at);
    for(const ev of hits){if(f.limit!=null&&n>=f.limit)break;n++;if(sent.has(ev.id))continue;sent.add(ev.id);this.fire('message',['EVENT',m[1],ev]);}}
   this.fire('message',['EOSE',m[1]]);},15);}
  if(m[0]==='EVENT'){__published.push(m[1]);setTimeout(()=>this.fire('message',['OK',m[1].id,true,'']),15);}}
 close(){this.readyState=3;this.fire('close',{})}
}
window.WebSocket=FixtureSocket;
'''

SEED = r'''(()=>{
  const sk=n=>new Uint8Array(32).fill(n), now=Math.floor(Date.now()/1000);
  const me=sk(1), mePk=NostrTools.getPublicKey(me), ev=[], pk=n=>NostrTools.getPublicKey(sk(n));
  const sign=(k,t,d)=>NostrTools.finalizeEvent(Object.assign({created_at:now-t,tags:[]},d),k);
  ev.push(sign(me,9000,{kind:0,content:JSON.stringify({name:'me'})}));
  ['alice','bob','carol','dave','erin'].forEach((nm,i)=>ev.push(sign(sk(i+2),9000,{kind:0,content:JSON.stringify({name:nm})})));
  // My follow list, so the home timeline has authors to ask about.
  ev.push(sign(me,8000,{kind:3,content:'',tags:[2,3,4,5,6].map(n=>['p',pk(n)])}));
  const ROOT=sign(me,5000,{kind:1,content:'THE ORIGINAL POST'});
  const R1=sign(sk(2),4000,{kind:1,content:'reply one from alice',tags:[['e',ROOT.id,'','root'],['p',mePk]]});
  const R2=sign(sk(3),3000,{kind:1,content:'reply two from bob, under alice',tags:[['e',ROOT.id,'','root'],['e',R1.id,'','reply'],['p',pk(2)],['p',mePk]]});
  const R3=sign(sk(4),2000,{kind:1,content:'reply three from carol, the deep one',tags:[['e',ROOT.id,'','root'],['e',R2.id,'','reply'],['p',pk(3)],['p',mePk]]});
  const R4=sign(sk(5),1500,{kind:1,content:'reply four from dave, to the original',tags:[['e',ROOT.id,'','root'],['p',mePk]]});
  const R5=sign(sk(6),1000,{kind:1,content:'reply five from erin, parent only',tags:[['e',R4.id],['p',pk(5)],['p',mePk]]});
  ev.push(ROOT,R1,R2,R3,R4,R5);
  window.__T={ROOT:ROOT.id,R1:R1.id,R2:R2.id,R3:R3.id,R4:R4.id,R5:R5.id};
  window.__events=ev;
  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(me);
  document.querySelector('#btn-nsec-login').click();
})()'''

# What the thread view shows once it has settled.
SHAPE = r'''(()=>{
  const T=__T, n=id=>document.querySelector('#feed .thread-node[data-tid="'+id+'"]');
  const inside=(c,p)=>!!(n(c)&&n(p)&&n(p)!==n(c)&&n(p).contains(n(c)));
  const hl=n(T.R3), feed=document.querySelector('#feed');
  const fr=feed.getBoundingClientRect(), hr=hl?hl.getBoundingClientRect():null;
  return {
    view: __PC.isView('thread'),
    have: Object.fromEntries(Object.entries(T).map(([k,id])=>[k,!!n(id)])),
    nested: {R1inRoot: !!document.querySelector('#feed .thread-top-replies .thread-node[data-tid="'+T.R1+'"]'),
             R2inR1: inside(T.R2,T.R1), R3inR2: inside(T.R3,T.R2), R5inR4: inside(T.R5,T.R4),
             R4notInR1: !inside(T.R4,T.R1)},
    rootFirst: (()=>{const o=[...document.querySelectorAll('#feed .thread-node[data-tid]')].map(e=>e.dataset.tid);return o[0]===T.ROOT})(),
    hl: !!(hl&&hl.classList.contains('thread-hl')),
    hlCount: document.querySelectorAll('#feed .thread-hl').length,
    inView: !!(hr && hr.top>=fr.top-2 && hr.top<fr.bottom-20),
    spinner: !!document.querySelector('#feed .spinner'),
    loadingRest: (document.querySelector('#feed')?.innerText||'').includes('Loading the rest'),
    count: (document.querySelector('#feed .search-section-title')?.textContent||''),
    errors: __errors,
  };
})()'''


async def scenario(width, check):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = os.environ.get('PC_CHECK_PORT')
    with tempfile.TemporaryDirectory(prefix='pc-notif-thread-') as profile:
        args = [CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
                '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank']
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                             {'width': width, 'height': 700, 'deviceScaleFactor': 1, 'mobile': width < 600})
                await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': INIT})
                await b.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/client'})
                await b.until("document.body?.classList.contains('guest')")
                await b.js(SEED)
                await b.until('!!window.__PC?.me()')
                await check(b)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
            server.server_close()


async def _settled(b):
    """Wait for the full thread, then a beat for anything that would replace it."""
    try:
        await b.until("!!document.querySelector('#feed .thread-node[data-tid=\"'+__T.R5+'\"]')"
                      " && !!document.querySelector('#feed .thread-node[data-tid=\"'+__T.ROOT+'\"]')")
    except Exception:
        pass
    await asyncio.sleep(1.5)
    return await b.js(SHAPE)


def _assert_whole_thread(got):
    assert got['view'], got
    assert all(got['have'].values()), ('posts of the conversation are missing', got)
    assert got['rootFirst'], ('the thread does not start at the original post', got)
    assert all(got['nested'].values()), ('a reply is not nested under the post it answers', got)
    assert got['hl'] and got['hlCount'] == 1, ('the clicked reply is not the one highlighted', got)
    assert got['inView'], ('the clicked reply is not scrolled into view', got)
    assert not got['spinner'] and not got['loadingRest'], ('the thread never finished loading', got)
    assert '5 replies' in got['count'], got
    assert not got['errors'], got


async def _open_notifications(b):
    await b.js("__PC.switchView('notifications')")
    await b.until("!!document.querySelector('#feed .notif[data-open=\"'+__T.R3+'\"]')")


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_a_deep_reply_notification_opens_the_whole_thread(width):
    async def check(b):
        await _open_notifications(b)
        assert await b.js("!!Store.get(__T.R3)"), 'the notification was not built from the Store'
        await b.js("document.querySelector('#feed .notif[data-open=\"'+__T.R3+'\"]').click()")
        _assert_whole_thread(await _settled(b))
    asyncio.run(scenario(width, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_a_notification_whose_post_left_the_store_still_opens_the_whole_thread():
    async def check(b):
        await _open_notifications(b)
        await b.js("Object.values(__T).forEach(id=>Store.removeEvent(id));true")
        assert not await b.js("!!Store.get(__T.R3)")
        await b.js("document.querySelector('#feed .notif[data-open=\"'+__T.R3+'\"]').click()")
        _assert_whole_thread(await _settled(b))
    asyncio.run(scenario(1280, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_a_reply_opened_from_the_timeline_signed_in_shows_the_whole_thread():
    async def check(b):
        await b.js("__PC.switchView('global')")
        await b.until("!!document.querySelector('#feed .note[data-id=\"'+__T.R3+'\"], #feed [data-open=\"'+__T.R3+'\"]')")
        await b.js("__PC.openThread(__T.R3);true")
        _assert_whole_thread(await _settled(b))
    asyncio.run(scenario(390, check))
