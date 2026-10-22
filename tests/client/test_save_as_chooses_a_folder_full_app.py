"""Save as lets you choose the folder: any folder in Files, any folder in any synced folder, or this computer.

2026-10-21: "Office Save as does not let me choose a folder! same for choosing synced folder or blossom". Files
saved to the drive's top level only, and Synced Folder saved into whichever folder happened to be open in Files
(and was missing otherwise). The real bundled client drives the sheet; the rules it must keep are pinned below.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop

ROOT = Path(__file__).resolve().parents[2]
FILES = (ROOT / "static/js/client/files.js").read_text()
NO_OS = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_browse_into_a_folder_make_one_and_save_there(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("__PC.switchView('blossom')")
        await asyncio.sleep(1)
        await b.js("(()=>{window.__saved=__PC.saveCopyTo(new Blob(['x']),'Season notes.odt');return 1})()")
        await b.until("!!document.querySelector('#sv-list .sv-row')")
        got['places'] = await b.js("[...document.querySelectorAll('#sv-list .sv-row b')].map(x=>x.textContent)")
        got['save_at_places'] = await b.js("document.querySelector('#sv-save').disabled")
        await b.js("document.querySelector('[data-sv-go=\"drive:\"]').click()")
        await b.js("document.querySelector('#sv-newdir').click()")
        await b.until("!!document.querySelector('.uiprompt-in')")
        await b.js("(()=>{const i=document.querySelector('.uiprompt-in');i.value='TV';document.querySelector('.uiconfirm [data-uc=\"1\"]').click();})()")
        await b.until("document.querySelector('#sv-crumbs').textContent.endsWith('TV')")
        got['crumbs'] = await b.js("document.querySelector('#sv-crumbs').textContent")
        await b.js("(()=>{const n=document.querySelector('#sv-name');n.value='Finale notes.odt';})()")
        got['fits'] = await b.js("(()=>{const m=document.querySelector('.modal').getBoundingClientRect();return m.left>=0&&m.right<=innerWidth+1})()")
        # Back out: a dismissed sheet settles with nothing (the caller's button must never stay stuck).
        await b.js("document.querySelector('#sv-cancel').click()")
        got['cancel'] = await b.js("window.__saved.then(v=>v===null)")
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    assert got['places'][0] == 'Files' and got['places'][-1] == 'This computer', got['places']
    assert got['save_at_places'] is True, "Save is offered before a place is chosen"
    assert got['crumbs'] == 'Places›Files›TV', got['crumbs']
    assert got['fits'] and got['cancel'] is True, got


def test_the_rules_the_sheet_keeps():
    store = FILES[FILES.index("async function _officeStoreDrive(blob,name,folder)"):][:900]
    # A document saved into an encrypted folder is uploaded encrypted, as Files' own upload does.
    assert "FilesIdx.isEncFolder(folder)" in store and "uploadEncFile(file,folder,null)" in store
    assert "FilesIdx.setFile(sha,{name,folder," in store, "the chosen folder must be what the drive index records"
    pick = FILES[FILES.index("function _chooseSaveTarget(name){"):][:9000]
    # Every synced folder is offered -- not only the one open in Files -- and its own sub-folders are browsable.
    assert "_syncPairs.map(f=>row(`data-sv-go=\"sync:" in pick and "_ensureSyncPairs()" in pick
    assert "_syncManifest(at.key)" in pick and "deletedAt" in pick and ".pc-trash" in pick
    assert "kind:'sync',key:at.key,dir:at.dir" in pick
    # Office's Save As and Save as PDF both go through it.
    assert FILES.count("await _officeSaveCopy(") >= 2
