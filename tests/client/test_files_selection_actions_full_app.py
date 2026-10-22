"""Files → your drive: selecting files and acting on them, in the SHIPPED bundle on a seeded three-file drive:

  * Select all selects what is on screen; Download saves each selected file;
  * Move → a folder moves every selected file there;
  * Delete (confirmed) sends ONE signed Blossom DELETE per selected file.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SHAS = ["1" * 64, "2" * 64, "3" * 64]

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__deletes=[];window.__saved=[];
const SHAS=%s;
const __ff=window.fetch;
window.fetch=(u,o)=>{const s=String(u),m=String((o&&o.method)||'GET').toUpperCase();
  const json=v=>Promise.resolve(new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}}));
  if(/\/list\/[0-9a-f]{64}/.test(s))return json(SHAS.map((h,i)=>({sha256:h,size:12+i,type:'text/plain',uploaded:1700000000+i,url:location.origin+'/blob/'+h})));
  const hit=SHAS.find(h=>s.includes(h));
  if(hit&&m==='DELETE'){__deletes.push({sha:hit,auth:(o.headers||{}).Authorization||''});return Promise.resolve(new Response('',{status:200}));}
  if(hit)return Promise.resolve(new Response('hello '+hit.slice(0,4),{status:200,headers:{'Content-Type':'text/plain'}}));
  return __ff(u,o);};
const __fa=HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click=function(){if(this.download){__saved.push(this.download);return;}return __fa.call(this);};
''' % (str(SHAS).replace("'", '"'))

SEED = r'''(()=>{const F=__PC.filesIdx();F._norm();SHAS.forEach((h,i)=>{F.data.files[h]={folder:'',name:'note'+i+'.txt',mime:'text/plain',ts:1700000000+i};});
  F.addFolder('Archive',false);F._pullDone=true;F._pullOk=true;F.saveLocal();return true;})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_select_all_download_move_and_delete():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; 1")
        await b.js(SEED)
        await b.js("__PC.switchView('blossom'); 1")
        # The drive opens on its home page; the three files are under "All files".
        await b.until("!!document.querySelector('.fx-home [data-folder=\"\"]')")
        await b.js("document.querySelector('.fx-home [data-folder=\"\"]').click(); 1")
        await b.until("document.querySelectorAll('.file-card[data-sha]').length>=3 && !!document.querySelector('#bl-selall')")
        await b.js("document.querySelector('#bl-selall').click(); 1")
        await b.until("!document.querySelector('#bl-seldl').disabled")
        await b.js("document.querySelector('#bl-seldl').click(); 1")
        await b.until("__saved.length===3")

        await b.js("document.querySelector('#bl-selmove').click(); 1")
        await b.until("!!document.querySelector('[data-m=\"Archive\"]')")
        await b.js("document.querySelector('[data-m=\"Archive\"]').click(); 1")
        await b.until("SHAS.every(h=>(__PC.filesIdx().meta(h)||{}).folder==='Archive')")

        await b.until("!!document.querySelector('#bl-selall')")
        await b.js("document.querySelector('#bl-selall').click(); 1")
        await b.until("!document.querySelector('#bl-seldel').disabled")
        n = await b.js("document.querySelectorAll('.file-card[data-sha]').length")
        await b.js("document.querySelector('#bl-seldel').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until(f"__deletes.length==={n}")
        assert all(d["auth"].startswith("Nostr ") for d in await b.js("__deletes")), "a DELETE went out unsigned"
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
