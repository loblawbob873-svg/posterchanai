"""The desktop's Virtual Machines shortcut opens the Virtual Machines screen — ONCE — with this computer first.

There used to be two doors to two different screens: the sidebar's "Virtual Machines" (the server hosts,
vms.js) and a desktop-only "Local VMs" (`__vms`, a separate painter in os.js). vms.js grew a LocalHost with
the same pcVM bridge ("This computer", always the first host), so the second screen is gone and `__vms`
is only an old NAME — a taskbar pin, a monitor handoff from an older shell — that must land on `vms`.

Runs the SHIPPED desktop bundle in Chrome (tests/client/test_desktop_offline_full_app.py's harness), with a
`pcVM` bridge that has one local machine. Asserted as a person meets it:
  * the start menu lists Virtual Machines once and no "Local VMs", and choosing it hands `vms` to the shell;
  * the desktop has one Virtual Machines icon; clicking it opens ONE window on the vms screen whose first
    host is "This computer" showing the local machine; clicking it again (and the old `__vms` name through
    openApp and switchView) focuses that window instead of opening another.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


LOCAL_MACHINE = r'''
window.pcVM={
  list:async()=>({available:true,machines:[{name:'debian-local',state:'shut off',cpus:2,ramMiB:2048,autostart:false}]}),
  details:async(n)=>({ok:true,name:n,state:'shut off',cpus:2,ramMiB:2048,autostart:false,disks:[{device:'disk',target:'vda'}],networks:1}),
  action:async()=>({ok:true}),view:async()=>({ok:true}),create:async()=>({ok:false}),remove:async()=>({ok:false}),
  update:async()=>({ok:true}),addDisk:async()=>({ok:true}),addNetwork:async()=>({ok:true}),changeIso:async()=>({ok:true}),
  ejectIso:async()=>({ok:true}),bootDisk:async()=>({ok:true}),gamingMouse:async()=>({ok:true}),pickIso:async()=>''};
window.__acts=[];window.pcPopup.act=async(a)=>{__acts.push(a);return true};
'''

# A window carries its identity in the frame, not an attribute: count frames titled Virtual Machines, and
# the old painter's markup (`.vmui`) must never be drawn again.
WINDOWS = ("[...document.querySelectorAll('#os-root .osw')].filter(w=>"
           "(w.querySelector('.osw-title')||{}).textContent==='Virtual Machines').length")
OTHER_VM_WINDOWS = "document.querySelectorAll('.vmui').length"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_start_menu_and_desktop_icon_open_virtual_machines_once_with_this_computer_first():
    async def check(b):
        await desktop.login(b)
        origin = await b.js('location.origin')

        # 1. The start menu (its own popup document): one Virtual Machines, no Local VMs.
        await b.call('Page.navigate', {'url': origin + '/index.html?pcpopup=start'})
        await b.until("!!document.querySelector('#os-startmenu .os-app[data-view=\"vms\"]')")
        rows = await b.js("[...document.querySelectorAll('#os-startmenu .os-app[data-view]')].map(x=>[x.dataset.view,x.textContent.trim()])")
        assert [r for r in rows if r[0] == 'vms'] == [['vms', 'Virtual Machines']], rows
        assert not [r for r in rows if r[0] == '__vms' or 'Local VMs' in r[1]], rows
        await b.js("document.querySelector('#os-startmenu .os-app[data-view=\"vms\"]').click()")
        await b.until("__acts.length>0")
        assert await b.js('__acts') == ['view:vms'], 'the start menu hands the shell the vms view'


        # 2. The desktop: one icon; a click opens one window on the vms screen, This computer first.
        await b.call('Page.navigate', {'url': origin + '/index.html'})
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-root .os-icons')")
        await b.until("!!document.querySelector('#os-root .os-icons .os-icon[data-view=\"vms\"]')")
        assert await b.js("document.querySelectorAll('#os-root .os-icons .os-icon[data-view=\"vms\"]').length") == 1
        assert await b.js("document.querySelectorAll('#os-root .os-icons .os-icon[data-view=\"__vms\"]').length") == 0
        await b.js("document.querySelector('#os-root .os-icons .os-icon[data-view=\"vms\"]').click()")
        await b.until(WINDOWS + "===1 && !!document.querySelector('.vms-host')")
        first = await b.js("(()=>{const h=document.querySelector('.vms-host');return {pk:h.dataset.host,text:h.innerText}})()")
        assert first['pk'] == 'local' and 'This computer' in first['text'], first
        await b.js("document.querySelector('.vms-host[data-host=\"local\"]').click()")
        await b.until("/debian-local/.test(document.querySelector('.vms').innerText)")

        # 3. Again, and by the OLD name through both funnels: still exactly one window, on vms.
        await b.js("document.querySelector('#os-root .os-icons .os-icon[data-view=\"vms\"]').click()")
        await b.js("PCOS.routeView('vms')")
        await asyncio.sleep(.6)
        assert await b.js(WINDOWS) == 1
        await b.js("__PC.switchView('__vms')")
        await asyncio.sleep(.6)
        assert await b.js("__PC.isView('vms')"), await b.js("__PC.currentView && __PC.currentView()")
        assert await b.js(WINDOWS) == 1 and await b.js(OTHER_VM_WINDOWS) == 0
        assert not await b.js("/Nothing here can show/.test(document.body.innerText)")
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, LOCAL_MACHINE))
