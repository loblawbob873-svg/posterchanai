"""Actual bundled Settings renderer with a deferred auth-boundary fixture; no signer requests."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop

@pytest.fixture(scope='module',autouse=True)
def bundle():
    generator=desktop.bundle.__wrapped__()
    next(generator)
    try:
        paths=list(desktop.BUNDLE_ROOT.rglob('app.js'))
        assert paths
        changed=0
        for path in paths:
            assert path.resolve().is_relative_to(desktop.BUNDLE_ROOT.resolve())
            source=path.read_text()
            start=source.find('  async function renderUserSettings(){')
            if start<0:continue
            at=source.index('await ensureAiSession();',start)
            source=source[:at]+'await window.__settingsAuthGate();'+source[at+len('await ensureAiSession();'):]
            source=source[:start]+'  window.__renderSettingsFixture=renderUserSettings;\n'+source[start:]
            path.write_text(source)
            changed+=1
        assert changed==1
        yield
    finally:
        try:next(generator)
        except StopIteration:pass

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
@pytest.mark.parametrize('outcome',['success','failure','navigate','account','edited'])
def test_empty_settings_status_and_guarded_completion(outcome):
    async def check(b):
        await desktop.login(b)
        await b.js("window.__settingsAuthGate=()=>new Promise((resolve,reject)=>{window.__settingsResolve=resolve;window.__settingsReject=reject});__PC.switchView('settings')")
        await b.until("!!document.querySelector('#user-settings [role=status]')")
        assert not await b.js("!!document.querySelector('#user-settings input,#user-settings select,#us-save')"),'loading state must not offer editable defaults'
        await b.until("document.querySelector('#user-settings [role=status]')?.textContent==='Establishing your app session…'")
        if outcome=='navigate':
            await b.js("__PC.switchView('home')")
        elif outcome=='account':
            await b.js("__PC.me().pubkey='b'.repeat(64)")
        elif outcome=='edited':
            # A newer real form/selection must not be replaced by this older completion.
            await b.js("document.querySelector('#user-settings').innerHTML='<input id=kept value=original>';document.querySelector('#kept').value='unsaved' ")
        await b.js("__settingsReject(new Error('fixture signer unavailable'))" if outcome=='failure' else "__settingsResolve({})")
        if outcome=='success':
            await b.until("!!document.querySelector('#us-save')")
            assert not await b.js("!!document.querySelector('#user-settings [data-settings-loading]')")
        elif outcome=='failure':
            await b.until("!!document.querySelector('#us-retry')")
            assert await b.js("document.querySelector('#user-settings').textContent.includes('fixture signer unavailable')")
            assert not await b.js("!!document.querySelector('#us-save')")
        else:
            await asyncio.sleep(1.2)
            assert not await b.js("!!document.querySelector('#us-save')")
            if outcome=='edited':assert await b.js("document.querySelector('#kept').value==='unsaved'")
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra))

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_existing_settings_edits_survive_refresh():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__settingsAuthGate=()=>Promise.resolve({});__PC.switchView('settings')")
        await b.until("!!document.querySelector('#us-save')")
        await b.js("window.keptSetting=[...document.querySelectorAll('#user-settings input')].find(el=>el.type==='text'||el.type==='email');if(!keptSetting)throw Error('real settings input missing');keptSetting.value='unsaved fixture';window.__settingsAuthGate=()=>new Promise(resolve=>window.__settingsResolve=resolve);void window.__renderSettingsFixture()")
        await asyncio.sleep(1.1)
        assert await b.js("keptSetting.isConnected&&keptSetting.value==='unsaved fixture'&&!document.querySelector('[data-settings-loading]')")
        await b.js("__settingsResolve({})")
        await asyncio.sleep(.4)
        assert await b.js("keptSetting.isConnected&&keptSetting.value==='unsaved fixture'"),'late response replaced existing edits'
    asyncio.run(desktop.with_browser('online','',check,"localStorage.setItem('pc_nostr_settings',JSON.stringify({osMode:false}));"))
