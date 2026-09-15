"""An early warm native tile survives the real client's unfinished boot."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop

@pytest.fixture(scope='module',autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
@pytest.mark.parametrize('view',['messages','notifications'])
def test_warm_tile_waits_for_boot_then_lands(view):
    async def check(b):
        await desktop.login(b)
        await b.js("localStorage.setItem('__holdLaunchConfig','1')")
        await b.call('Page.reload')
        await b.until("!!window.__PC && !!window.__launchListener && typeof __releaseLaunchConfig==='function'")
        assert not await b.js('!!window.__PC_BOOTED')
        await b.js(f"__parkedLaunch='{view}';__launchListener({{view:'{view}'}})")
        await b.until('__launchTakes>0')
        assert not await b.js('!!window.__PC_BOOTED')
        assert not await b.js(f"__PC.isView('{view}')"), 'native route ran before boot finished'
        await b.js('__releaseLaunchConfig()')
        await b.until('!!window.__PC_BOOTED')
        await b.until(f"__PC.isView('{view}') && !!document.querySelector('#feed')?.children.length")
        assert await b.js('__parkedLaunch') == ''
    asyncio.run(desktop.with_browser('online','',check,extra_init=r'''
window.__parkedLaunch='';window.__launchTakes=0;
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=> 'android',Plugins:{HomeScreen:{
 consumeLaunchView:async()=>{__launchTakes++;const view=__parkedLaunch;__parkedLaunch='';return {view};},
 addListener:(name,fn)=>{if(name==='launchView')window.__launchListener=fn;return {remove(){}};}
}}};
const launchFetch=window.fetch;
window.fetch=(url,opts)=>{
 if(localStorage.getItem('__holdLaunchConfig') && String(url).includes('/client/config'))
  return new Promise(resolve=>{window.__releaseLaunchConfig=()=>resolve(launchFetch(url,opts));});
 return launchFetch(url,opts);
};
'''))
