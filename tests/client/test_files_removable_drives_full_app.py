"""Files shows, mounts, opens, writes to and ejects USB drives on PosterChanOS.

"important, we need to make sure files can see/mount/read/write removeable drives like USB". The
shipped bundle in a PosterChanOS Files window, with the drives bridge (desktop/drives.js) and the host
file bridge stood in; their real halves are tests/test_desktop_drives.py and the hostfs tests.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
window.__drives=[{dev:'/dev/sdb1',disk:'/dev/sdb',label:'STICK',fstype:'vfat',size:32014630912,mountpoint:''},
                 {dev:'/dev/sdc',disk:'/dev/sdc',label:'Backup',fstype:'exfat',size:1000204886016,mountpoint:'/run/media/alice/Backup'}];
window.__mounted=[];window.__ejected=[];window.__writes=[];window.__driveCb=null;
window.pcDrives={
  list:async()=>__drives.map(d=>({...d})),
  mount:async(dev)=>{__mounted.push(dev);const d=__drives.find(x=>x.dev===dev);d.mountpoint='/run/media/alice/'+d.label;return {dev,path:d.mountpoint}},
  unmount:async(dev)=>({dev,unmounted:true}),
  eject:async(dev)=>{__ejected.push(dev);__drives=__drives.filter(x=>x.dev!==dev);return {dev,ejected:true}},
  onChange:(cb)=>{__driveCb=cb;return ()=>{}},
};
const FILES={'/home/test':[{name:'notes.txt',path:'/home/test/notes.txt',dir:false,size:10,mtime:1}],
  '/run/media/alice/STICK':[{name:'holiday.jpg',path:'/run/media/alice/STICK/holiday.jpg',dir:false,size:2048,mtime:1},
                            {name:'todo.txt',path:'/run/media/alice/STICK/todo.txt',dir:false,size:12,mtime:1}]};
window.pcHost={roots:async()=>[{path:'/home/test',name:'Home',kind:'home'}],
  list:async(path)=>({path,parent:path==='/'?'':'/',entries:FILES[path]||[]}),
  read:async()=>new TextEncoder().encode('buy milk'), readText:async()=>({text:'buy milk',mtime:1}),
  writeText:async(p,text,mtime)=>{__writes.push([p,text]);return {ok:true,mtime:2}}, open:async()=>({ok:true})};
window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;
'''

CHIPS = "[...document.querySelectorAll('.fx-side [data-drive]')].map(b=>b.textContent.trim())"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_usb_stick_is_listed_opened_written_and_ejected():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('.fx-side')")
        await b.until("document.querySelectorAll('.fx-side [data-drive]').length===2")
        got['chips'] = await b.js(CHIPS)
        got['eject_before'] = await b.js("[...document.querySelectorAll('[data-drive-eject]')].map(x=>x.dataset.driveEject)")
        # Open the stick: it is mounted, then its own files are shown.
        await b.js("document.querySelector('[data-drive=\"/dev/sdb1\"]').click()")
        await b.until("!!document.querySelector('[data-p=\"/run/media/alice/STICK/holiday.jpg\"]')")
        got['mounted'] = await b.js("__mounted.slice()")
        got['at'] = await b.js("PCHostFiles.at()")
        # Write to it, through the same bridge the editor saves with.
        got['write'] = await b.js("PCHostFiles.writeText('/run/media/alice/STICK/todo.txt','buy milk and bread',1).then(r=>r.ok)")
        got['writes'] = await b.js("__writes.slice()")
        # A stick plugged in while Files is open appears by itself.
        await b.js("__drives.push({dev:'/dev/sdd1',disk:'/dev/sdd',label:'CAMERA',fstype:'exfat',size:64000000000,mountpoint:''});__driveCb&&__driveCb();true")
        await b.until("!!document.querySelector('.fx-side [data-drive=\"/dev/sdd1\"]')")
        got['after_plug'] = await b.js(CHIPS)
        # Eject the open stick: safe-removal, and Files goes back home.
        await b.until("!!document.querySelector('[data-drive-eject=\"/dev/sdb1\"]')")
        await b.js("document.querySelector('[data-drive-eject=\"/dev/sdb1\"]').click()")
        await b.until("__ejected.length===1 && !document.querySelector('.fx-side [data-drive=\"/dev/sdb1\"]')")
        await b.until("!!document.querySelector('[data-p=\"/home/test/notes.txt\"]')")
        got['ejected'] = await b.js("__ejected.slice()")
        got['toast'] = await b.js("(window.toasts||[]).some(t=>/can be removed safely/.test(String(t)))||document.body.innerText.includes('can be removed safely')")

    asyncio.run(desktop.with_browser('online', '?pcwin=blossom', check, FIXTURE))
    assert any('STICK' in c for c in got['chips']) and any('Backup' in c for c in got['chips']), got['chips']
    assert got['eject_before'] == ['/dev/sdc'], ("only a mounted drive offers eject", got['eject_before'])
    assert got['mounted'] == ['/dev/sdb1'] and got['at'] == '/run/media/alice/STICK', got
    assert got['write'] is True and got['writes'] == [['/run/media/alice/STICK/todo.txt', 'buy milk and bread']], got
    assert any('CAMERA' in c for c in got['after_plug']), ("a stick plugged in did not appear", got['after_plug'])
    assert got['ejected'] == ['/dev/sdb1'] and got['toast'], got
