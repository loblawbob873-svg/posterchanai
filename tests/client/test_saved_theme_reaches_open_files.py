"""Save a theme in Settings while a separate native Files window stays open."""
import asyncio
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_effects_full_app import Browser


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INIT = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.pcHost={roots:async()=>[{path:'/home/test/Reports',name:'Reports',kind:'home'}],
 list:async(path)=>({path,parent:'/home/test',entries:[{name:'report.odt',path:'/home/test/Reports/report.odt',dir:false,size:4,mtime:100}]})};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_saved_theme_updates_open_files_without_reloading_or_losing_directory():
    async def check(settings):
        await desktop.login(settings)
        await settings.js("__PC.switchView('settings')")
        await settings.until("!!document.querySelector('#us-theme')")
        origin = await settings.js('location.origin')
        target = await settings.call('Target.createTarget', {'url':'about:blank'})
        port = settings.ws.remote_address[1]
        async with httpx.AsyncClient() as client:
            pages = (await client.get(f'http://127.0.0.1:{port}/json')).json()
        page = next(row for row in pages if row['id'] == target['targetId'])
        async with websockets.connect(page['webSocketDebuggerUrl'], max_size=20_000_000) as socket:
            files = Browser(socket)
            await files.call('Page.enable')
            await files.call('Network.enable')
            await files.call('Network.setBlockedURLs', {'urls':['https://*','wss://*']})
            script = desktop.NETWORK_FIXTURE + desktop.OFFLINE.replace('MODE','online') + INIT
            script += "window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;"
            await files.call('Page.addScriptToEvaluateOnNewDocument', {'source':script})
            await files.call('Page.navigate', {'url':origin+'/index.html?pcwin=blossom'})
            await files.until("!!window.__PC && (__PC.me() || document.body.classList.contains('guest'))")
            if not await files.js('!!__PC.me()'):
                await desktop.login(files)
            await files.js("__PC.switchView('blossom')")
            await files.until("!!document.querySelector('[data-host=\"1\"]')")
            await files.js("document.querySelector('[data-host=\"1\"]').click()")
            await files.until("!!document.querySelector('[data-p=\"/home/test/Reports/report.odt\"]')")
            await files.js("window.__filesRoot=document.querySelector('.fx-explorer');window.__filesDocument=document;window.__themeEvents=0;window.addEventListener('storage',e=>{if(e.key==='pc_theme')__themeEvents++})")
            assert await files.js('PCOSWin.isWindow()')
            for theme in ['professional','cyberpunk','dark']:
                before = await files.js("document.documentElement.getAttribute('data-theme') || 'cyberpunk'")
                await settings.js(f"document.querySelector('#us-theme').value='{theme}';document.querySelector('#us-theme').dispatchEvent(new Event('change',{{bubbles:true}}))")
                # Unsaved previews belong to Settings only.
                await asyncio.sleep(.1)
                assert await files.js("document.documentElement.getAttribute('data-theme') || 'cyberpunk'") == before
                await settings.js("document.querySelector('#us-save').click()")
                await files.until(f"(document.documentElement.getAttribute('data-theme') || 'cyberpunk') === '{theme}'")
                assert await files.js("document===__filesDocument && document.querySelector('.fx-explorer')===__filesRoot")
                assert await files.js('PCHostFiles.at()') == '/home/test/Reports'
                assert await files.js("!!document.querySelector('[data-p=\"/home/test/Reports/report.odt\"]')")
            assert await files.js('__themeEvents') == 3, 'received themes must not be written back in a loop'
    asyncio.run(desktop.with_browser('online','',check,INIT))
