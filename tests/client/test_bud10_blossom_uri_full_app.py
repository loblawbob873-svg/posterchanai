"""BUD-10: A `blossom:` URI IN A POST IS SHOWN AS THE FILE IT NAMES, AND ONLY IF THE BYTES ARE THAT FILE.

"looks like we are missing bud-10 support" (2026-10-30): a post holding
`blossom:<sha256>.jpg?xs=blossom.laantungir.net&xs=cdn.satellite.earth&xs=blossom.primal.net&as=<author>&sz=117475`
drew as a line of text. The shipped bundle draws three posts from servers stubbed at fetch():
  * the first hinted server answers with the WRONG bytes, the second with the right ones -> the right ones are shown;
  * no server hints, only `as=` -> the author's own Blossom list (kind 10063) is asked;
  * the only server cannot be read (no CORS, as blossom.laantungir.net measured) -> shown as a plain image.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
           "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

# A real 1x1 PNG and a different one; the page hashes them itself.
SETUP = r'''(async()=>{const me=__PC.me();
  const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
  const good=b64('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==');
  const bad=b64('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==');
  const hex=async u=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',u))].map(x=>x.toString(16).padStart(2,'0')).join('');
  const sha=await hex(good); window.__sha=sha; window.__fetched=[];
  const png=u=>new Response(u,{status:200,headers:{'content-type':'image/png'}});
  const f0=window.fetch;
  window.fetch=function(url,opts){const u=String(url);
    if(u.startsWith('https://liar.example/')){__fetched.push(u);return Promise.resolve(png(bad));}
    if(u.startsWith('https://honest.example/')||u.startsWith('https://authors.example/')){__fetched.push(u);return Promise.resolve(png(good));}
    if(u.startsWith('https://nocors.example/')){__fetched.push(u);return Promise.reject(new TypeError('Failed to fetch'));}
    return f0.apply(this,arguments);};
  // a plain <img> from the no-CORS server works (what an ordinary image link does): serve it from a data: URL
  const st=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'src');
  Object.defineProperty(HTMLImageElement.prototype,'src',{get(){return st.get.call(this);},set(v){
    if(String(v).startsWith('https://nocors.example/')){this.dataset.plain=v;v='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';}
    st.set.call(this,v);}});
  const now=Math.floor(Date.now()/1000);
  const sign=(c,t)=>__PC.signTemplate({kind:1,pubkey:me.pubkey,created_at:t,tags:[],content:c});
  const posts=[await sign('first: blossom:'+sha+'.png?xs=liar.example&xs=honest.example&sz=70',now-10),
               await sign('second: blossom:'+sha+'.png?as='+me.pubkey,now-20),
               await sign('third: blossom:'+sha+'.png?xs=nocors.example',now-30)];
  const list=await __PC.signTemplate({kind:10063,pubkey:me.pubkey,created_at:now,tags:[['server','https://authors.example']],content:''});
  posts.forEach(e=>Store.saveEvent(e));
  const q=Relay.query.bind(Relay);
  Relay.query=async(f,...r)=>{const fl=Array.isArray(f)?f:[f];
    if(fl.some(x=>(x.kinds||[]).includes(10063))){const a=[list];a.complete=true;return a;}
    if(fl.some(x=>(x.authors||[]).includes(me.pubkey)&&(x.kinds||[]).includes(1))){const a=posts.slice();a.complete=true;return a;}
    return q(f,...r);};
  return me.pubkey})()'''

SHOWN = r'''[...document.querySelectorAll('#prof-list article, #prof-list .note')].map(n=>{
  const i=n.querySelector('img.bud10'); const t=n.textContent;
  return {which:(t.match(/(first|second|third):/)||[])[1]||'', img:!!i, state:i?i.dataset.bud10State||'':'',
          blob:!!(i&&/^blob:/.test(i.src)), osrc:i?i.dataset.osrc||'':'', plain:i?i.dataset.plain||'':'',
          text:/blossom:[0-9a-f]{64}/.test(t), w:i?i.naturalWidth:0};}).filter(x=>x.which)'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_blossom_uri_is_drawn_from_a_server_whose_bytes_match_its_hash():
    got = {}

    async def check(b):
        await desktop.login(b)
        pk = await b.js(SETUP)
        await b.js(f"__PC.openProfile('{pk}')")
        await b.until("document.querySelectorAll('#prof-list article, #prof-list .note').length>=3")
        for _ in range(60):
            s = await b.js(SHOWN)
            if len(s) == 3 and all(x['state'] in ('ok', 'unverified', 'failed') for x in s):
                break
            await asyncio.sleep(.2)
        got['shown'] = {x['which']: x for x in await b.js(SHOWN)}
        got['fetched'] = await b.js("__fetched")

    asyncio.run(desktop.with_browser('online', '', check, CLASSIC))
    s = got['shown']
    assert set(s) == {'first', 'second', 'third'}, s
    for k in s:
        assert s[k]['img'] and not s[k]['text'], ('the blossom: URI was drawn as text, not as its image', s[k])
    assert s['first']['state'] == 'ok' and s['first']['blob'] and s['first']['osrc'].startswith('https://honest.example/'), \
        ('the wrong bytes from the first server were shown, or the right ones were not', s['first'], got['fetched'])
    assert s['second']['state'] == 'ok' and s['second']['osrc'].startswith('https://authors.example/'), \
        ("the author's own Blossom list (kind 10063) was not asked", s['second'], got['fetched'])
    assert s['third']['state'] == 'unverified' and s['third']['plain'].startswith('https://nocors.example/'), \
        ('a server without CORS left the post with no picture', s['third'])
    assert all(x['w'] > 0 for x in s.values()), ('an image did not actually decode', s)
