"""POSTERCHANOS, NO NETWORK: A DOCUMENT OPENS IN OFFICE ON THE MACHINE.

"for posterchanOS, let's make office work offline too". Files → This Computer → a .odt → Office, in the
real client, with NO instance (every /client/office request fails like a dead network). The desktop's
bridge `pcOfficeLocal` (desktop/office-local.js — driven for real by tests/test_office_local.py) is the
boundary here: the editor must be launched from the session IT returned, Save must write the editor's
bytes back to the file, Close must release the session, and the instance must never be asked.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BOUNDARIES = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__instanceAsked=0;window.__opened=[];window.__closed=0;window.__writes=[];
window.pcHost={
 roots:async()=>[{path:'/home/test',name:'Home',kind:'home'}],
 list:async(path)=>({path,parent:path==='/home/test'?'/home':'/home/test',entries:path==='/home/test/Reports'
   ?[{name:'report.odt',path:'/home/test/Reports/report.odt',dir:false,size:4,mtime:100}]
   :[{name:'Reports',path:'/home/test/Reports',dir:true,size:0,mtime:100}]}),
 read:async()=>new TextEncoder().encode('test'),
 writeBytes:async(path,bytes,mtime)=>{__writes.push([path,new TextDecoder().decode(bytes)]);return {ok:true,mtime:200};},
 open:async()=>{throw Error('must open Office inside the app');},
};
window.pcOfficeLocal={installed:true,available:async()=>true,
 open:async(bytes,name,mode)=>{__opened.push([new TextDecoder().decode(new Uint8Array(bytes)),name,mode]);
   return {ok:true,id:'local1',token:'tok',expires:9999999999,editor_url:location.origin+'/__local-editor'};},
 contents:async(id,token)=>({ok:true,bytes:new TextEncoder().encode('edited on this machine')}),
 export:async()=>({ok:false,error:'not in this test'}),
 close:async()=>{__closed++;return {ok:true,closed:true};}};
const realFetch=window.fetch;
window.fetch=async function(url,opts={}){
 if(String(url).includes('/client/office/')){__instanceAsked++;throw new TypeError('Failed to fetch');}
 return realFetch(url,opts);
};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_offline_a_document_opens_saves_and_closes_on_the_machine():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('blossom')")
        await b.until("!!document.querySelector('[data-host=\"1\"]')")
        await b.js("document.querySelector('[data-host=\"1\"]').click()")
        await b.until("!!document.querySelector('[data-p=\"/home/test/Reports\"]')")
        await b.js("document.querySelector('[data-p=\"/home/test/Reports\"]').click()")
        await b.until("!!document.querySelector('[data-p=\"/home/test/Reports/report.odt\"]')")
        await b.js("document.querySelector('[data-p=\"/home/test/Reports/report.odt\"]').click()")
        await b.until("!!document.querySelector('[data-ow=office]')")
        await b.js("document.querySelector('[data-ow=office]').click()")
        await b.until("!!document.querySelector('.office-view #office-save')")
        got['action'] = await b.js("document.querySelector('.office-launch').getAttribute('action')")
        got['token'] = await b.js("document.querySelector('.office-launch [name=access_token]').value")
        await b.js("document.querySelector('#office-save').click()")
        await b.until("__writes.length>0")
        await b.js("document.querySelector('#office-close').click()")
        await b.until("__closed>0")
        got.update(await b.js("({opened:__opened,writes:__writes,closed:__closed,asked:__instanceAsked,errors:__errors})"))

    asyncio.run(desktop.with_browser('offline', '', check, BOUNDARIES))
    assert got['opened'] == [['test', 'report.odt', 'edit']], got
    assert got['action'].endswith('/__local-editor') and got['token'] == 'tok', got
    assert got['writes'] == [['/home/test/Reports/report.odt', 'edited on this machine']], got
    assert got['closed'] == 1 and got['asked'] == 0, got
