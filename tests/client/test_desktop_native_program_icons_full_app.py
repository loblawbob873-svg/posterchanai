"""An installed program (Steam, Firefox) can be put on the PosterChanOS desktop, and stays there.

Reported as a missing feature: "we need ability to add desktop icons for OS apps like steam,
firefox, etc for posterchanOS". The start menu already listed every `.desktop` program under This
computer and the taskbar could pin one; the desktop could hold only the client's own screens.

Runs the SHIPPED bundle. The only fixtures are the boundaries: the compositor/app-scan bridges
(`pcWM`, `pcApps`, `pcPopup`) and a relay that keeps what is published in localStorage, so a reload
reads back exactly what was written. Asserted end to end, because every half of this fails without
saying so:

  * the start menu popup HANDS the choice to the desktop window (the popup closes the moment it is
    made, so a save begun inside it would die with it);
  * the desktop draws the program with its own picture and STARTS it through the launcher, never
    opening it as a client screen;
  * a reload reads the icon back from the encrypted layout document;
  * a device with no machine (a browser, the APK) draws no programs and, when it saves an
    unrelated change, does not strip them from the account's document.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# A relay that REMEMBERS (replaceable 30078 by d-tag), and a machine with two installed programs.
# `localStorage.__noMachine` turns the machine off for the reload that plays "a browser".
MACHINE = r'''
window.__publishOK=true;
window.__events=JSON.parse(localStorage.getItem('__relayEvents')||'[]');
window.__keep=(ev)=>{
  const d=(ev.tags.find(t=>t[0]==='d')||[])[1];
  window.__events=window.__events.filter(x=>!(d&&x.kind===ev.kind&&x.pubkey===ev.pubkey&&
    (x.tags.find(t=>t[0]==='d')||[])[1]===d));
  window.__events.push(ev);
  localStorage.setItem('__relayEvents',JSON.stringify(window.__events));
};
const _fixtureSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){
  try{ const m=JSON.parse(raw); if(Array.isArray(m)&&m[0]==='EVENT'&&m[1].kind===30078) __keep(m[1]); }catch(_){}
  return _fixtureSend.call(this,raw);
};
window.__launches=[];window.__acts=[];window.__wmListeners=[];
if(!localStorage.getItem('__noMachine')){
  window.pcApps={list:async()=>({apps:[
    {id:'firefox-bin',name:'Firefox',comment:'Web browser',argv:['/usr/bin/firefox-bin'],match:'firefox-bin',
     iconUri:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='},
    {id:'steam',name:'Steam',comment:'Games',argv:['/usr/bin/steam'],match:'steam',group:'Games'}]})};
  window.pcWM={windows:async()=>[],onEvent:(fn)=>{__wmListeners.push(fn);return()=>{}},
    launch:async(argv)=>{__launches.push(argv);return {pid:1,window:{id:77}}},
    focus:async()=>true};
  window.pcPopup.act=async(a)=>{__acts.push(a);return true};
}
'''


async def _icon(b, key):
    return await b.js("!!document.querySelector('#os-root .os-icons .os-icon[data-view=\"" + key + "\"]')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_installed_program_is_put_on_the_desktop_started_from_it_and_kept():
    async def check(b):
        await desktop.login(b)
        origin = await b.js('location.origin')

        # 1. The start menu, in its own popup window: right-click Firefox → Add to desktop.
        await b.call('Page.navigate', {'url': origin + '/index.html?pcpopup=start'})
        await b.until("!!document.querySelector('#os-startmenu .os-app[data-app=\"app:firefox-bin\"]')")
        await b.js("document.querySelector('#os-startmenu .os-app[data-app=\"app:firefox-bin\"]')"
                   ".dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:40,clientY:40}))")
        await b.until("[...document.querySelectorAll('.os-ctx button')].some(x=>x.textContent.trim()==='Add to desktop')")
        await b.js("[...document.querySelectorAll('.os-ctx button')].find(x=>x.textContent.trim()==='Add to desktop').click()")
        await b.until('__acts.length>0')
        acts = await b.js('__acts')
        assert acts == ['desk-add:' + 'app%3Afirefox-bin%0AFirefox'], acts
        assert not await b.js("__events.some(e=>e.kind===30078)"), \
            'the popup must hand the change to the desktop, not write the layout itself'

        # 2. The desktop window performs it, as main.js delivers it: a shell tick.
        await b.call('Page.navigate', {'url': origin + '/index.html'})
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-root .os-icons')")
        await b.until('__wmListeners.length>0')
        for _ in range(40):
            await b.js("__wmListeners.forEach(f=>f({name:'tick',payload:'pc:act:" + acts[0] + "'}))")
            await asyncio.sleep(.3)
            if await _icon(b, 'app:firefox-bin'):
                break
        assert await _icon(b, 'app:firefox-bin'), await b.js("document.querySelector('#os-root').innerText.slice(0,600)")
        tile = await b.js("""(()=>{const t=document.querySelector('.os-icon[data-view="app:firefox-bin"]');
            const img=t.querySelector('img.os-app-ic');const r=(img||t).getBoundingClientRect();
            // The same measurement on a client screen's glyph: a program's picture is not smaller.
            const g=[...document.querySelectorAll('#os-root .os-icons .os-icon:not(.is-native):not(.is-folder) > svg.ic')][0];
            const gr=g.getBoundingClientRect();
            return {label:t.querySelector('span').textContent.trim(),img:!!img&&img.src.startsWith('data:'),
                    w:r.width,h:r.height,gw:gr.width,gh:gr.height}})()""")
        assert tile['label'] == 'Firefox' and tile['img'], tile
        assert abs(tile['w'] - tile['gw']) < 1 and abs(tile['h'] - tile['gh']) < 1, tile

        # 3. Clicking it STARTS the program through the launcher; no client window opens for it.
        await b.js("document.querySelector('.os-icon[data-view=\"app:firefox-bin\"]').click()")
        await b.until('__launches.length===1')
        assert await b.js('__launches[0]') == ['/usr/bin/firefox-bin']
        assert not await b.js("!!document.querySelector('.osw[data-view=\"app:firefox-bin\"]')")

        # 4. Reload: read back from the relay.
        await b.until("__events.some(e=>e.kind===30078)")
        await b.call('Page.reload')
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-root .os-icons')")
        await b.until("!!document.querySelector('#os-root .os-icon[data-view=\"app:firefox-bin\"]')")

        # 5. The same account on a device with no machine: nothing drawn, nothing lost on save.
        await b.js("localStorage.setItem('__noMachine','1')")
        await b.call('Page.reload')
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-root .os-icons')")
        await asyncio.sleep(1.5)
        assert not await _icon(b, 'app:firefox-bin'), 'a browser cannot start a PosterChanOS program'
        before = await b.js("__events.filter(e=>e.kind===30078).map(e=>e.id)")
        # An unrelated change, made the way a person makes it: right-click an icon → Hide.
        victim = await b.js("""(()=>{const t=[...document.querySelectorAll('#os-root .os-icons .os-icon')]
            .find(x=>!x.classList.contains('is-folder')&&!x.classList.contains('is-native'));return t.dataset.view})()""")
        for _ in range(40):          # the first tries can land before the layout is writable
            await b.js("document.querySelector('#os-root .os-icons .os-icon[data-view=\"" + victim + "\"]')"
                       "?.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:60,clientY:60}))")
            await b.js("[...document.querySelectorAll('.os-ctx button')].find(x=>x.textContent.trim()==='Hide from desktop')?.click()")
            await asyncio.sleep(.4)
            if await b.js("__events.filter(e=>e.kind===30078).map(e=>e.id).join()") != ','.join(before):
                break
        after = await b.js("__events.filter(e=>e.kind===30078).map(e=>e.id)")
        assert after != before, 'the unrelated save never happened, so nothing was proven'
        doc = await b.js("""(async()=>{const ev=__events.find(e=>e.kind===30078);
            return JSON.parse(await __PC.nip44dec(__PC.me().pubkey, ev.content))})()""")
        assert [n['key'] for n in doc.get('native', [])] == ['app:firefox-bin'], doc
        assert 'app:firefox-bin' in doc.get('order', []), doc
        assert victim in doc.get('hidden', []), doc
        assert 'Add a program…' not in await b.js("(()=>{const d=document.querySelector('#os-desk');d.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:d.getBoundingClientRect().right-40,clientY:d.getBoundingClientRect().bottom-40}));return [...document.querySelectorAll('.os-ctx button')].map(x=>x.textContent.trim())})()"), 'no programs to add on a browser'
        await b.js("document.querySelector('.os-ctx')?.remove()")

        # 6. Back on the machine it is still there.
        await b.js("localStorage.removeItem('__noMachine')")
        await b.call('Page.reload')
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-root .os-icons')")
        await b.until("!!document.querySelector('#os-root .os-icon[data-view=\"app:firefox-bin\"]')")

        # 7. The desktop's own way in: right-click the wallpaper → Add a program… → Steam.
        rows = await b.js("(()=>{const d=document.querySelector('#os-desk');d.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:d.getBoundingClientRect().right-40,clientY:d.getBoundingClientRect().bottom-40}));return [...document.querySelectorAll('.os-ctx button')].map(x=>x.textContent.trim())})()")
        assert 'Add a program…' in rows, rows
        await b.js("[...document.querySelectorAll('.os-ctx button')].find(x=>x.textContent.trim()==='Add a program…').click()")
        await b.until("!!document.querySelector('.os-apppick .os-app[data-app=\"app:steam\"]')")
        assert await b.js("document.querySelector('.os-apppick .os-app[data-app=\"app:firefox-bin\"]').disabled"), \
            'a program already on the desktop is not offered twice'
        await b.js("document.querySelector('.os-apppick .os-app[data-app=\"app:steam\"]').click()")
        await b.until("!!document.querySelector('#os-root .os-icon[data-view=\"app:steam\"]')")
        await b.until("""(async()=>{const ev=__events.find(e=>e.kind===30078);
            const d=JSON.parse(await __PC.nip44dec(__PC.me().pubkey, ev.content));
            return d.native.map(n=>n.key).join()==='app:firefox-bin,app:steam'})()""")

        # 8. Right-click a program icon → Remove from desktop.
        await b.js("document.querySelector('#os-root .os-icon[data-view=\"app:steam\"]')"
                   ".dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:80,clientY:80}))")
        await b.js("[...document.querySelectorAll('.os-ctx button')].find(x=>x.textContent.trim()==='Remove from desktop').click()")
        await b.until("!document.querySelector('#os-root .os-icon[data-view=\"app:steam\"]')")
        await b.until("""(async()=>{const ev=__events.find(e=>e.kind===30078);
            const d=JSON.parse(await __PC.nip44dec(__PC.me().pubkey, ev.content));
            return d.native.map(n=>n.key).join()==='app:firefox-bin' && !d.order.includes('app:steam')})()""")
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, MACHINE))
