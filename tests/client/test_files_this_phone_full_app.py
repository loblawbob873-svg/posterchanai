"""Files shows THIS PHONE's own storage on Android: find it, grant access, browse, preview, hand a file to another app.

Asked 2026-10-28: "android: no way to browse user's android storage on files". The native half (PhoneFilesCore's rules,
run on a real directory; the plugin compiled against the Android SDK) is tests/test_android_phone_files.py. This drives
the SHIPPED bundle at a phone's size with the `PhoneFiles` plugin as a fixture over an in-memory phone:

  * Files offers "This phone" (home tile and sidebar) -- and nothing claims to be "My Computer";
  * before access is granted it says so with an "Allow access" button, never an empty phone; granting and coming back
    lists the storage;
  * a folder opens; a picture previews from the phone's bytes; any other file can go to "Another app" (the plugin's
    open, with that file's path);
  * a browser (no plugin) shows no "This phone" at all.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__phone={granted:false,asked:0,opened:[],
  fs:{'/storage/emulated/0':['Download','DCIM','notes.txt'],
      '/storage/emulated/0/Download':['photo.png','backup.zip'],
      '/storage/emulated/0/DCIM':[]}};
// Installed AFTER sign-in: present at boot, a Capacitor object sends the page down the native sign-in path.
window.__installPhone=()=>{ const P=__phone, R='/storage/emulated/0';
  const isDir=p=>Object.prototype.hasOwnProperty.call(P.fs,p);
  window.Capacitor={isNativePlatform:()=>true,Plugins:{PhoneFiles:{
    access:async()=>({granted:P.granted,api:34}),
    requestAccess:async()=>{P.asked++;return {opened:true};},
    roots:async()=>({roots:[{name:'Internal storage',path:R,kind:'home'}]}),
    list:async({dir})=>{ if(!P.granted) throw new Error('denied'); if(!isDir(dir)) throw new Error('not a folder');
      return {path:dir,parent:dir===R?null:dir.slice(0,dir.lastIndexOf('/')),
        entries:P.fs[dir].map(n=>{const p=dir+'/'+n;return {name:n,path:p,dir:isDir(p),size:isDir(p)?0:68,mtime:1700000000000,created:0,hidden:false};})}; },
    read:async({path})=>({b64:'PNGB64',path,size:68,mtime:1700000000000}),
    open:async({path})=>{P.opened.push(path);return {ok:true};},
  }}}; };
'''.replace('PNGB64', PNG)


async def _files_home(b):
    await b.js("__PC.switchView('blossom');true")
    await b.until("!!document.querySelector('#feed .fx-home-tile[data-hosthome]') || !!document.querySelector('#feed .fx-home-tile')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_this_phone_is_browsable_after_access_is_granted():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 3, 'mobile': True})
        await desktop.login(b)
        await b.js("__installPhone();true")
        await _files_home(b)
        await b.until("!!document.querySelector('#feed .fx-home-tile[data-hosthome]')")
        got['home'] = await b.js("document.querySelector('#feed').innerText")
        await b.js("document.querySelector('#feed .fx-home-tile[data-hosthome]').click();true")

        # Not granted yet: said, with the way to grant it -- never an empty phone.
        await b.until("!!document.querySelector('.hf-access .hf-grant')")
        got['ask'] = await b.js("document.querySelector('.hf-access').innerText")
        await b.js("document.querySelector('.hf-grant').click();true")
        await b.until("__phone.asked===1")
        # The person turns it on in Settings and comes back to the app.
        await b.js("__phone.granted=true;document.dispatchEvent(new Event('visibilitychange'));true")
        await b.until("[...document.querySelectorAll('.file-card[data-p]')].length===3")
        got['root'] = await b.js("[...document.querySelectorAll('.file-card[data-p] .fname')].map(x=>x.textContent)")
        got['label'] = await b.js("document.body.innerText.includes('This phone')")
        got['computer'] = await b.js("[...document.querySelectorAll('.fx-tree-head b')].map(x=>x.textContent)")

        # Into a folder.
        await b.js("document.querySelector('.file-card[data-p=\"/storage/emulated/0/Download\"]').click();true")
        await b.until("!!document.querySelector('.file-card[data-p=\"/storage/emulated/0/Download/photo.png\"]')")

        # A picture previews from the phone's own bytes.
        await b.js("document.querySelector('.file-card[data-p=\"/storage/emulated/0/Download/photo.png\"] .fname').click();true")
        await b.until("!!document.querySelector('.pv-host img') || !!document.querySelector('.ow-opt')")
        if await b.js("!!document.querySelector('.ow-opt[data-ow=\"preview\"]')"):
            await b.js("document.querySelector('.ow-opt[data-ow=\"preview\"]').click();true")
        await b.until("!!document.querySelector('.pv-host img')")
        got['preview'] = True
        await b.js("(document.querySelector('.pv-host .pv-x')||{click(){}}).click();true")
        await asyncio.sleep(.4)

        # Anything else can go to another app on the phone.
        await b.until("!!document.querySelector('.file-card[data-p=\"/storage/emulated/0/Download/backup.zip\"]')")
        await b.js("document.querySelector('.file-card[data-p=\"/storage/emulated/0/Download/backup.zip\"] .fname').click();true")
        await b.until("!!document.querySelector('.ow-opt[data-ow=\"host\"]')")
        got['another'] = await b.js("document.querySelector('.ow-opt[data-ow=\"host\"]').innerText")
        await b.js("document.querySelector('.ow-opt[data-ow=\"host\"]').click();true")
        await b.until("__phone.opened.length===1")
        got['opened'] = await b.js("__phone.opened")
        got['errors'] = await b.js("window.__errors||[]")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
    assert 'this phone' in got['home'].lower() and 'this computer' not in got['home'].lower(), got['home']
    assert 'permission' in got['ask'] and 'Allow access' in got['ask'], got['ask']
    assert sorted(got['root']) == ['DCIM', 'Download', 'notes.txt'], got['root']
    assert 'This phone' in got['computer'] and 'My Computer' not in got['computer'], got['computer']
    assert got['preview']
    assert 'Another app' in got['another'], got['another']
    assert got['opened'] == ['/storage/emulated/0/Download/backup.zip'], got['opened']
    assert not got['errors'], got['errors']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_browser_has_no_this_phone():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 3, 'mobile': True})
        await desktop.login(b)
        await _files_home(b)
        await asyncio.sleep(.5)
        got['phone'] = await b.js("!!window.pcPhoneHost")
        got['text'] = await b.js("document.querySelector('#feed').innerText")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
    assert got['phone'] is False and 'this phone' not in got['text'].lower(), got
