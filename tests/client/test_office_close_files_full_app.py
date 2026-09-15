"""Open Office from a real Files row, then close through the shipped router and renderer."""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BOUNDARIES = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__officeDeletes=0;
window.__hostReads=[];
window.pcHost={
 roots:async()=>[{path:'/home/test',name:'Home',kind:'home'}],
 list:async(path)=>({path,parent:path==='/home/test'?'/home':'/home/test',entries:path==='/home/test/Reports'
   ?[{name:'report.odt',path:'/home/test/Reports/report.odt',dir:false,size:4,mtime:100}]
   :[{name:'Reports',path:'/home/test/Reports',dir:true,size:0,mtime:100}]}),
 read:async(path)=>{__hostReads.push(path);return new TextEncoder().encode('test');},
 open:async()=>{throw Error('must open Office inside the app');},
};
const officeFetch=window.fetch;
window.fetch=async function(url,opts={}){
 const u=String(url);
 if(u.includes('/client/office/session')){
  if(opts.method==='DELETE'){__officeDeletes++;return new Response('{}',{status:200});}
  return new Response(JSON.stringify({id:'test-session',token:'test-token',expires:9999999999,
   editor_url:location.origin+'/__test-editor'}),{status:200,headers:{'Content-Type':'application/json'}});
 }
 return officeFetch(url,opts);
};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('native_window', [False, True])
def test_office_close_restores_the_same_files_source_and_directory(native_window):
    async def check(browser):
        await desktop.login(browser)
        await browser.js("__PC.switchView('blossom')")
        await browser.until("!!document.querySelector('[data-host=\"1\"]')")
        await browser.js("document.querySelector('[data-host=\"1\"]').click()")
        await browser.until("!!document.querySelector('[data-p=\"/home/test/Reports\"]')")
        await browser.js("document.querySelector('[data-p=\"/home/test/Reports\"]').click()")
        await browser.until("!!document.querySelector('[data-p=\"/home/test/Reports/report.odt\"]')")
        assert await browser.js("PCHostFiles.at()") == '/home/test/Reports'
        if native_window:
            assert await browser.js('PCOSWin.isWindow()')
        assert not await browser.js('PCOS.isOn()'), 'must exercise full-feed editor, not independent OS document'
        await browser.js("document.querySelector('[data-p=\"/home/test/Reports/report.odt\"]').click()")
        await browser.until("!!document.querySelector('[data-ow=office]')")
        await browser.js("document.querySelector('[data-ow=office]').click()")
        await browser.until("!!document.querySelector('.office-view #office-close')")
        await browser.js("document.querySelector('#office-close').click()")
        await browser.until("!!document.querySelector('#host-pane [data-p=\"/home/test/Reports/report.odt\"]')")
        assert await browser.js("__PC.VIEW") == 'blossom'
        assert await browser.js("PCHostFiles.at()") == '/home/test/Reports'
        assert await browser.js("!!document.querySelector('[data-fxtoggle=computer].active')")
        assert not await browser.js("!!document.querySelector('.office-view, #office-new-doc')")
        assert await browser.js('__officeDeletes') == 1
        assert await browser.js('__hostReads') == ['/home/test/Reports/report.odt']

    extra = BOUNDARIES
    if native_window:
        extra += "window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;"
    asyncio.run(desktop.with_browser('online', '?pcwin=blossom' if native_window else '', check, extra))
