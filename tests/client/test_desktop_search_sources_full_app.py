"""The taskbar Search looks through every source System Settings → Search switches on, in its order.

Reported as three requests: "TaskBar -> Change Search Nostr to Search", "System Settings -> Search ->
enable/disable Nostr Search or show local files or Notes first", and "add ability to search Files
and Notes". Runs the SHIPPED bundle; the only fixtures are the boundaries -- the relay (which keeps
what is published, so the setting's round trip through the encrypted `pcai:desktop` document is
real), the compositor bridge and this computer's disk (`pcHost`).

What is asserted, end to end, because each half fails without saying so:
  * the box says "Search", not "Search Nostr";
  * the Settings page writes the choice into the ACCOUNT's layout document (it follows the person);
  * with Nostr switched off the Search window asks no relay for anything, and with Notes moved to
    the top the notebook's hit is the first thing in the window;
  * every source actually returns its hit -- a note decrypted here, a drive file from the encrypted
    index, a file on this computer from the disk walk, an app;
  * each result opens the THING: the note opens in Notes, the drive file offers its openers (and
    "Show in Files"), the local file offers Code / This computer and reaches the machine.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
window.__publishOK=true;
window.__events=JSON.parse(localStorage.getItem('__relayEvents')||'[]');
window.__keep=(ev)=>{
  const d=(ev.tags.find(t=>t[0]==='d')||[])[1];
  window.__events=window.__events.filter(x=>!(d&&x.kind===ev.kind&&x.pubkey===ev.pubkey&&
    (x.tags.find(t=>t[0]==='d')||[])[1]===d));
  window.__events.push(ev);
  localStorage.setItem('__relayEvents',JSON.stringify(window.__events));
};
window.__searchReqs=[];
const _fixtureSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){
  try{ const m=JSON.parse(raw);
       if(Array.isArray(m)&&m[0]==='EVENT'&&m[1].kind===30078) __keep(m[1]);
       if(Array.isArray(m)&&m[0]==='REQ'&&m.slice(2).some(f=>f&&f.search)) __searchReqs.push(m); }catch(_){}
  return _fixtureSend.call(this,raw);
};
window.__hostOpened=[];
window.pcWM={windows:async()=>[],onEvent:()=>()=>{},focus:async()=>true,launch:async()=>({pid:1}),
  shellFront:async()=>true};
window.pcHost=Object.assign(window.pcHost||{},{
  search:async(q)=>String(q).toLowerCase().includes('budget')
    ? [{name:'budget-2026.conf',path:'/home/me/Documents/budget-2026.conf',dir:false}] : [],
  open:async(p)=>{__hostOpened.push(p);return {ok:true}},
});
'''

QUERY = 'budget'


async def _boot(b):
    await desktop.login(b)
    await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar')")
    await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")


async def _search(b, q):
    await b.js("(()=>{const i=document.querySelector('#os-q-bar');i.focus();i.value=" + json.dumps(q) + ";"
               "i.dispatchEvent(new Event('input',{bubbles:true}));"
               "i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))})()")


SECTIONS = "[...document.querySelectorAll('#feed .os-srch-sec:not([hidden])')].map(s=>s.dataset.source)"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_taskbar_search_follows_system_settings_and_opens_what_it_finds():
    async def check(b):
        await _boot(b)
        box = await b.js("(()=>{const i=document.querySelector('#os-q-bar');return [i.placeholder,i.getAttribute('aria-label')]})()")
        assert box == ['Search', 'Search'], box

        # Something in each source to find: a note in the encrypted notebook, a file in the drive's
        # encrypted index. (The disk is the pcHost fixture.)
        await b.js("__PC.notesSearch('warm-up')")
        await b.until('!!window.PCNotes')
        for _ in range(40):
            if await b.js("(async()=>{try{await PCNotes.save({title:'Budget meeting',body:'bring the "
                          "spreadsheet'});return true}catch(e){return false}})()"):
                break
            await asyncio.sleep(.3)
        await b.js("""(()=>{const F=__PC.filesIdx();F._pullDone=true;
            F.data.files['ab'.repeat(32)]={name:'budget-plan.xyz',folder:'Money',mime:'',enc:1};})()""")

        # System Settings → Search: Nostr off, Notes to the top.
        await b.js("PCOS.openSystemSettings()")
        await b.until("!!document.querySelector('.os-set-nav [data-page=\"search\"]')")
        await b.js("document.querySelector('.os-set-nav [data-page=\"search\"]').click()")
        await b.until("!!document.querySelector('[data-search-prefs] [data-src-on=\"nostr\"]')")
        order = await b.js("[...document.querySelectorAll('[data-search-prefs] .os-srch-pref')].map(r=>r.dataset.src)")
        assert order == ['nostr', 'apps', 'notes', 'files', 'local'], order
        for _ in range(30):   # the first tries can land before the layout is writable
            await b.js("(()=>{const c=document.querySelector('[data-search-prefs] [data-src-on=\"nostr\"]');"
                       "if(c&&c.checked){c.checked=false;c.dispatchEvent(new Event('change'))}})()")
            await asyncio.sleep(.4)
            if await b.js("!document.querySelector('[data-search-prefs] [data-src-on=\"nostr\"]').checked"):
                break
        for _ in range(4):
            await b.js("document.querySelector('[data-search-prefs] [data-src-up=\"notes\"]:not([disabled])')?.click()")
            await asyncio.sleep(.3)
        order = await b.js("[...document.querySelectorAll('[data-search-prefs] .os-srch-pref')].map(r=>r.dataset.src)")
        assert order[0] == 'notes', order
        # …and it is the ACCOUNT's decision: it is in the encrypted layout document on the relay.
        await b.until("""(async()=>{const ev=__events.find(e=>e.kind===30078&&e.tags.some(t=>t[1]==='pcai:desktop'));
            if(!ev)return false;const d=JSON.parse(await __PC.nip44dec(__PC.me().pubkey,ev.content));
            return !!d.search&&d.search.order[0]==='notes'&&d.search.off.includes('nostr')})()""")

        # The taskbar search: every local source, Notes first, and no relay asked anything.
        await _search(b, QUERY)
        await b.until("[...document.querySelectorAll('#feed .os-srch-row')].length>=3")
        await b.until(SECTIONS + ".join()==='notes,files,local'||" + SECTIONS + ".join()==='notes,apps,files,local'")
        secs = await b.js(SECTIONS)
        assert secs[0] == 'notes', secs
        assert 'nostr' not in secs
        assert await b.js("__searchReqs.length") == 0, 'Nostr is switched off, yet a relay was searched'
        assert not await b.js("[...document.querySelectorAll('#feed .search-section-title')].some(t=>/^posts$/i.test(t.textContent.trim()))")
        first = await b.js("document.querySelector('#feed .os-srch-row b').textContent")
        assert first == 'Budget meeting', first

        # A drive result opens the FILE (its openers), with the folder one choice away.
        await b.js("document.querySelector('#feed .os-srch-sec[data-source=\"files\"] .os-srch-row').click()")
        await b.until("!!document.querySelector('.openwith .ow-opt')")
        opts = await b.js("[...document.querySelectorAll('.openwith .ow-opt b')].map(x=>x.textContent)")
        assert 'Show in Files' in opts and 'PosterChan Code' in opts, opts
        await b.js("document.querySelector('#ow-x')?.click()")
        await asyncio.sleep(.3)

        # A file on this computer offers the editor and the machine's own opener, and the latter works.
        await b.js("document.querySelector('#feed .os-srch-sec[data-source=\"local\"] .os-srch-row').click()")
        await b.until("!!document.querySelector('.openwith .ow-opt[data-ow=\"host\"]')")
        await b.js("document.querySelector('.openwith .ow-opt[data-ow=\"host\"]').click()")
        await b.until('__hostOpened.length===1')
        assert await b.js('__hostOpened[0]') == '/home/me/Documents/budget-2026.conf'

        # The note opens in Notes, on that note.
        await b.js("document.querySelector('#feed .os-srch-sec[data-source=\"notes\"] .os-srch-row').click()")
        await b.until("document.querySelector('.nt-title')?.value==='Budget meeting'")

        # Nostr back on, first: the relay is asked again.
        await b.js("PCOS.openSystemSettings()")
        await b.until("!!document.querySelector('[data-search-prefs] [data-src-on=\"nostr\"]')")
        await b.js("(()=>{const c=document.querySelector('[data-search-prefs] [data-src-on=\"nostr\"]');"
                   "c.checked=true;c.dispatchEvent(new Event('change'))})()")
        await b.until("document.querySelector('[data-search-prefs] [data-src-on=\"nostr\"]').checked")
        await _search(b, QUERY)
        await b.until('__searchReqs.length>0')
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_file_found_in_the_start_menu_opens():
    """The start menu listed "Files on this computer" (and the drive's files) and a click on one opened
    NOTHING: the row's own onclick was assigned and then REPLACED by the handler every `.os-app` row
    gets, which fell through to `openLauncherApp(undefined)`. The start menu is a popup, so the choice
    must also travel to the desktop window -- closing the popup first cancels the very IPC that would
    have opened the file."""
    async def check(b):
        await desktop.login(b)
        origin = await b.js('location.origin')
        await b.call('Page.navigate', {'url': origin + '/index.html?pcpopup=start'})
        await b.until("!!document.querySelector('#os-startmenu #os-q')")
        await b.js("window.__acts=[];pcPopup.act=async(a)=>{__acts.push(a);return true}")
        await b.js("(()=>{const q=document.querySelector('#os-q');q.value='budget';"
                   "q.dispatchEvent(new Event('input',{bubbles:true}))})()")
        await b.until("!!document.querySelector('#os-startmenu .os-app[data-path]')")
        await b.js("document.querySelector('#os-startmenu .os-app[data-path]').click()")
        await b.until('__acts.length>0')
        assert await b.js('__acts[0]') == 'path:%2Fhome%2Fme%2FDocuments%2Fbudget-2026.conf%0A', await b.js('__acts')

        # The desktop window performs it: a file on this computer gets its openers.
        await b.call('Page.navigate', {'url': origin + '/index.html'})
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("__PC.hostOpen('/home/me/Documents/budget-2026.conf','budget-2026.conf','',false)")
        await b.until("!!document.querySelector('.openwith .ow-opt[data-ow=\"host\"]')")
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
