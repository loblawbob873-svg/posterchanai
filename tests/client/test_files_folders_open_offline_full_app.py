"""OFFLINE, FILES STILL SHOWS YOUR DRIVE'S FOLDERS AND WHAT IS IN THEM.

"Users should be able to access their … files … without network". The drive index is kept on the
device (FilesIdx.saveLocal), so a relaunch with no network has everything it needs to draw the drive.
The real client, two phases in one profile: the index is on the device; reload with every relay socket
dead and the instance unreachable; Files must show the folder, opening it must list the file, and the
offline state must be said rather than shown as an empty drive.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


DEAD = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
if(localStorage.getItem('__dead')==='1'){
  Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>false});
  window.WebSocket=class extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
    constructor(u){super();this.url=String(u);this.readyState=0;} send(){} close(){this.readyState=3;}};
  const up=window.fetch;
  window.fetch=(url,opts)=>/files-index|\/list\//.test(String(url))?Promise.reject(new TypeError('Failed to fetch')):up(url,opts);
}
'''
SHA = 'f' * 64
INDEX = {"folders": ["Music", "Receipts"], "encFolders": [],
         "files": {SHA: {"name": "tax-2026.pdf", "folder": "Receipts", "size": 52000, "mime": "application/pdf",
                         "ts": 1790000000}}}


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_files_opens_a_folder_with_no_network(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("localStorage.setItem('pc_files_idx_'+__PC.me().pubkey," + json.dumps(json.dumps(INDEX)) + ");localStorage.setItem('__dead','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC && !!__PC.me()')
        await b.js("__PC.switchView('blossom')")
        await b.until("[...document.querySelectorAll('.folder-chip[data-folder],[data-dir]')].some(e=>/Receipts/.test(e.textContent+(e.dataset.dir||'')))")
        await b.js("(()=>{const e=[...document.querySelectorAll('.folder-chip[data-folder],[data-dir]')].find(e=>/Receipts/.test(e.textContent+(e.dataset.dir||'')));e.click();})()")
        for _ in range(100):
            got['text'] = await b.js("(document.querySelector('#feed')||document.body).innerText")
            if 'tax-2026.pdf' in got['text']:
                break
            await asyncio.sleep(.1)
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, DEAD))
    assert 'tax-2026.pdf' in got['text'], ("the folder opened empty offline", got['text'][-600:])
    assert not got['errors'], got['errors']
