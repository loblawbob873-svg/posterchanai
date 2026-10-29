"""A link whose address ends in a hash is a link, and a picture from somebody else's host never gets our session.

poster.place, 2026-10-29: the post-deploy sign-in check failed on "Access to fetch at 'https://plebeian.market/products/
<64 hex>' ... blocked by CORS policy". A post linking a market product was drawn as an <img> -- four copies of the
"64 hex characters at the end means a Blossom blob" rule all said so -- the image failed (it is a web page), and the
fallback re-fetched it from the browser WITH this instance's cookies and Bearer token. Now one rule decides
(app.js _blossomBlobUrl: the hash is the path, or sits under a storage segment; a post's own `r` reference is a link),
and the fallback sends credentials only to this instance.

The shipped client, the real post (from poster.place's relay) served by a stub relay, every fetch recorded.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
H = "9f2ef9a107f21683bfc3aa239d9f5208ff67cd99a3f21ac06269089857190a7f"
PRODUCT = "https://plebeian.market/products/" + H
FOREIGN_BLOB = "https://blossom.example.invalid/" + "c" * 64          # a real blob shape on someone else's host
POSTS = [
    {"kind": 1, "id": "97e46d2be24c" + "0" * 52, "pubkey": "64bfa9abffe5b18d0731eed57b38173adc2ba89bf87c168da90517f021e722b5",
     "created_at": 1793270000, "sig": "",
     "content": 'Check out "Howth Photography Print" on Plebeian!\n\n' + PRODUCT + "\n\n#plebeian",
     "tags": [["a", "30402:64bfa9abffe5b18d0731eed57b38173adc2ba89bf87c168da90517f021e722b5:" + H], ["r", PRODUCT], ["t", "plebeian"]]},
    {"kind": 1, "id": "11" * 32, "pubkey": "22" * 32, "created_at": 1793269000, "sig": "",
     "content": "a picture from another blossom\n" + FOREIGN_BLOB, "tags": []},
]
INIT = r'''
window.__fetches=[];
const __f=window.fetch;window.fetch=function(u,o){const s=String(u&&u.url||u);
  if(!/\/(static|client\/config|client\/stats|api\/)/.test(s)||/plebeian|example\.invalid/.test(s))
    __fetches.push({url:s,credentials:(o&&o.credentials)||'',auth:!!(o&&o.headers&&o.headers.Authorization)});
  return __f.apply(this,arguments);};
const __POSTS=%s;
class FS{constructor(u){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1];
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  for(const f of m.slice(2)){ if((f.kinds||[]).includes(1)&&!f.ids&&!f.authors&&!f['#e']&&!f['#p']){ __POSTS.forEach(e=>out(['EVENT',id,e])); } }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FS.OPEN=1;FS.CONNECTING=0;FS.CLOSED=3;window.WebSocket=FS;
''' % json.dumps(POSTS)


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@CHROME
def test_a_product_link_is_a_link_and_a_foreign_picture_never_gets_our_session():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('global')")
        await b.until("document.body.innerText.includes('Howth Photography')")
        await asyncio.sleep(4)
        got['imgs'] = await b.js("[...document.querySelectorAll('#tl-notes img')].map(i=>i.getAttribute('src')||'').filter(s=>s.includes('plebeian'))")
        got['links'] = await b.js("[...document.querySelectorAll('#tl-notes a')].map(a=>a.getAttribute('href')||'').filter(s=>s.includes('plebeian.market/products'))")
        got['fetches'] = await b.js("__fetches")
        got['errors'] = await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['imgs'] == [], ("the product page was drawn as an image", got['imgs'])
    assert got['links'], "the product link is not a link"
    direct = [f for f in got['fetches'] if f['url'].startswith(PRODUCT)]
    assert direct == [], ("the browser fetched the product page itself", direct)
    tried = [f for f in got['fetches'] if 'example.invalid' in f['url']]
    assert tried, "the foreign picture's fallback never ran -- the session check below would prove nothing"
    leaked = [f for f in got['fetches'] if 'example.invalid' in f['url'] and (f['credentials'] == 'include' or f['auth'])]
    assert leaked == [], ("a picture on somebody else's host was fetched with this instance's session", leaked)
