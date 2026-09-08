"""Bundled Desktop UI survives a missing instance, using real client scripts/DOM.

Only instance HTTP, relay WebSocket and preload IPC boundaries are fixtures. Bundle
assembly runs unchanged; browser navigation/static files remain available locally.
PC_OFFLINE_APP_ROOT selects another isolated source checkout without patching it.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import httpx
import pytest
import websockets
from tests.client.test_effects_full_app import Browser, INIT as NETWORK_FIXTURE

ROOT=Path(os.environ.get('PC_OFFLINE_APP_ROOT',Path(__file__).resolve().parents[2]))
BUNDLE_ROOT=None
OFFLINE=r'''
window.__instanceMode=localStorage.getItem('__offlineMode')||'MODE';window.__waitingHTTP=[];window.__instanceFailures=0;
window.__documentIdentity=crypto.randomUUID();window.__picked=[];
window.__localStarts=[];window.__localWrites=[];window.__localListeners=[];window.__localText='offline shell ready\r\n$ ';
window.pcTerm={start:async(opts)=>{__localStarts.push(opts);return{id:'fixture-local'}},
 write:async(id,data)=>{__localWrites.push(data);__localText+=data;__localListeners.forEach(fn=>fn({id,d:data,seq:__localText.length}));return true},
 resize:async()=>true,backlog:async()=>({alive:true,d:__localText,seq:__localText.length}),
 close:async()=>true,list:async()=>[],attach:async()=>true,detach:async()=>true,
 onData:fn=>{__localListeners.push(fn);return()=>{__localListeners=__localListeners.filter(x=>x!==fn)}}};
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:true}));
window.pcShell={instanceSync:'https://fixture.invalid',instanceChosenSync:true,getInstance:async()=> 'https://fixture.invalid',setInstance:async()=>true,retry:()=>{throw Error('unexpected native reload')}};
window.pcPopup={pick:async(view)=>{__picked.push(view);return true},act:async()=>true,close:async()=>true};
const onlineFetch=window.fetch;
window.fetch=function(url,opts={}){
 const u=String(url),local=new URL(u,location.href);
 if(local.origin===location.origin&&(local.pathname.startsWith('/static/')||local.pathname==='/sw.js'||local.pathname==='/index.html'))return onlineFetch(url,opts);
 if(__instanceMode==='reject'){__instanceFailures++;return Promise.reject(new TypeError('fixture instance unavailable'));}
 if(__instanceMode==='hang'){__instanceFailures++;return new Promise((resolve,reject)=>{
   __waitingHTTP.push(()=>onlineFetch(url,opts).then(resolve,reject));
   if(opts.signal)opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
 });}
 if(u.includes('/api/instance-welcome/access'))return Promise.resolve(new Response(JSON.stringify({qualified:true,pubkey:window.__PC?.me()?.pubkey}),{status:200,headers:{'Content-Type':'application/json'}}));
 return onlineFetch(url,opts);
};
window.__restoreInstance=()=>{__instanceMode='online';__waitingHTTP.splice(0).forEach(f=>f());window.dispatchEvent(new Event('online'));};
'''


class BundleHandler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(BUNDLE_ROOT),**kwargs)
    def log_message(self,*args):pass


@pytest.fixture(scope='module',autouse=True)
def bundle():
    global BUNDLE_ROOT
    # Keep generated www/resources private to this module. Full-suite checks also assemble
    # Desktop, so sharing ROOT/desktop/www can delete files underneath another browser.
    with tempfile.TemporaryDirectory(prefix='pc-offline-bundle-') as directory:
        source=Path(directory)
        for name in ('static','templates','scripts'):(source/name).symlink_to(ROOT/name,target_is_directory=True)
        (source/'desktop').mkdir()
        shutil.copyfile(ROOT/'desktop/build-www.sh',source/'desktop/build-www.sh')
        subprocess.run(['bash',str(source/'desktop/build-www.sh')],cwd=source,check=True,capture_output=True,text=True,timeout=60)
        BUNDLE_ROOT=source/'desktop/www'
        yield
        BUNDLE_ROOT=None


async def with_browser(mode,route,check,extra_init=""):
    server=ThreadingHTTPServer(('127.0.0.1',0),BundleHandler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-offline-desktop-') as profile:
        proc=subprocess.Popen(['/opt/google/chrome/chrome','--headless=new','--no-sandbox','--disable-gpu','--window-size=1440,1000','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile,'DevToolsActivePort').exists():break
                await asyncio.sleep(.1)
            port=Path(profile,'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:pages=(await h.get(f'http://127.0.0.1:{port}/json')).json()
            async with websockets.connect(next(p for p in pages if p.get('type')=='page')['webSocketDebuggerUrl'],max_size=20_000_000) as ws:
                b=Browser(ws)
                await b.call('Page.enable');await b.call('Network.enable')
                await b.call('Network.setBlockedURLs',{'urls':['https://*','wss://*']})
                await b.call('Page.addScriptToEvaluateOnNewDocument',{'source':NETWORK_FIXTURE.replace('nostr_only:false,relay_url:', 'nostr_only:(window.__freshPolicy??false),relay_url:')+OFFLINE.replace('MODE',mode)+extra_init})
                await b.call('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/index.html'+route})
                await b.until('!!window.__PC && !!window.PCOS')
                await check(b)
        finally:
            proc.terminate();proc.wait(timeout=10);server.shutdown();server.server_close()


async def cold_popup(mode,kind):
    async def check(b):
        # Measured from local scripts being available, not Chrome process startup. A menu must
        # paint while config is still pending; the old 2.5s network gate exposes a black window.
        expression="(()=>{const h=document.querySelector('#os-popup-host');return !!h&&h.getBoundingClientRect().height>30&&h.innerText.trim().length>10})()"
        started=await b.js('performance.now()')
        for _ in range(10):
            if await b.js(expression):break
            await asyncio.sleep(.1)
        assert await b.js(expression),await b.js("({errors:__errors,mode:__instanceMode,pending:__waitingHTTP.length,html:document.documentElement.className,body:document.body.innerText.slice(-500)})")
        elapsed=await b.js('performance.now()')-started
        assert await b.js('__instanceFailures>0')
        if mode=='hang':assert await b.js('__waitingHTTP.length>0')
        if kind=='start':
            assert await b.js("document.querySelectorAll('#os-startmenu button').length>4")
        token=await b.js('__documentIdentity')
        await b.js('__restoreInstance()')
        await asyncio.sleep(.3)
        assert await b.js('__documentIdentity')==token
        assert await b.js(expression)
        print(f'{kind} {mode}: populated popup in {elapsed:.0f}ms; reconnect kept document')
    await with_browser(mode,'?pcpopup='+kind,check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
@pytest.mark.parametrize('kind',['start','net','noti'])
@pytest.mark.parametrize('mode',['hang','reject'])
def test_native_popup_is_usable_before_instance_config(mode,kind):
    asyncio.run(cold_popup(mode,kind))


async def login(b):
    await b.until("document.body.classList.contains('guest')")
    await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
    await b.until('!!__PC.me()')


async def running_offline():
    async def check(b):
        await login(b)
        await b.until("!!document.querySelector('#os-start')")
        token=await b.js('__documentIdentity')
        await b.js("document.querySelector('#os-start').click()")
        await b.until("!!document.querySelector('#os-q')")
        await b.js("(()=>{const q=document.querySelector('#os-q');q.value='Notes';q.dispatchEvent(new Event('input',{bubbles:true}))})()")
        await b.until("!!document.querySelector('#os-startmenu [data-view=notes]')")
        await b.js("document.querySelector('#os-startmenu [data-view=notes]').click()")
        await b.until("!!document.querySelector('.nt-new')")
        await b.js("__instanceMode='hang';window.dispatchEvent(new Event('offline'));document.querySelector('.nt-new').click()")
        await b.until("!!document.querySelector('.nt-body')")
        await b.js("(()=>{const ta=document.querySelector('.nt-body');ta.value='Work kept while the instance is unreachable';ta.focus();ta.setSelectionRange(9,13);ta.dispatchEvent(new Event('input',{bubbles:true}));window.__workingNote=ta})()")
        # Start a shell through the real Start menu and Terminal UI. Only the native PTY
        # IPC boundary is a fixture; no server answer is needed for the local machine.
        await b.js("document.querySelector('#os-start').click()")
        await b.until("!!document.querySelector('#os-q')")
        await b.js("(()=>{const q=document.querySelector('#os-q');q.value='Terminal';q.dispatchEvent(new Event('input',{bubbles:true}))})()")
        await b.until("!!document.querySelector('#os-startmenu [data-view=terminal]')")
        await b.js("document.querySelector('#os-startmenu [data-view=terminal]').click()")
        await b.until("!!document.querySelector('#tty-host option[value=local]')")
        await b.until('__localStarts.length===1')
        await b.until("!!document.querySelector('.xterm-helper-textarea')")
        await b.js("window.__terminalElement=document.querySelector('.xterm');document.querySelector('.xterm-helper-textarea').focus()")
        await b.call('Input.insertText',{'text':'echo still working'})
        await b.until("__localWrites.join('').includes('echo still working')")
        assert await b.js("__workingNote.value==='Work kept while the instance is unreachable'")
        assert await b.js("document.querySelector('#os-bar').getBoundingClientRect().height>30")
        await b.js('__restoreInstance()')
        await asyncio.sleep(3)
        assert await b.js('__documentIdentity')==token,'reconnect reloaded the document'
        assert await b.js("document.querySelector('.nt-body').value==='Work kept while the instance is unreachable'"),'reconnect lost local note text'
        assert await b.js("__localStarts.length===1 && __terminalElement.isConnected && __localText.includes('echo still working')"),'reconnect replaced the local terminal'
        await b.js("document.querySelector('#os-start').click()")
        await b.until("!!document.querySelector('#os-startmenu')")
        assert not await b.js('__errors')
    await with_browser('online','',check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_running_desktop_keeps_local_terminal_and_note_when_instance_disappears():
    asyncio.run(running_offline())


async def saved_popup(cached_policy):
    async def check(b):
        await login(b)
        await b.js("localStorage.setItem('__offlineMode','hang');localStorage.setItem('pc_client_config',JSON.stringify({nostr_only:"+json.dumps(cached_policy)+",relay_url:'wss://fixture.invalid',relays:['wss://fixture.invalid']}))")
        url=await b.js("location.origin+'/index.html?pcpopup=start'")
        await b.call('Page.navigate',{'url':url})
        await b.until("!!window.__PC && !!document.querySelector('#os-q')")
        await b.js("(()=>{const q=document.querySelector('#os-q');q.value='AI';q.focus();q.dispatchEvent(new Event('input',{bubbles:true}));q.setSelectionRange(0,1);window.__search=q})()")
        assert await b.js("!!document.querySelector('#os-startmenu [data-view=ai]')")== (not cached_policy)
        token=await b.js('__documentIdentity')
        # Canonicalizing the document URL must not change its native-popup identity. The
        # History API changes the route without replacing the loaded renderer, as startup does.
        await b.js("history.replaceState(history.state,'',location.pathname);PCOS.restore()")
        assert await b.js("!document.querySelector('#os-root') && __search.isConnected && document.querySelector('#os-q')===__search && __search.value==='AI' && __search.selectionStart===0 && __search.selectionEnd===1")
        await b.js('__restoreInstance()')
        await b.until('!!__PC.me()')
        await b.until("!!document.querySelector('#os-startmenu [data-view=ai]')==="+json.dumps(cached_policy))
        assert await b.js("document.querySelector('#os-q')===__search && __search.value==='AI' && document.activeElement===__search && __search.selectionStart===0 && __search.selectionEnd===1"),'late config/login lost popup search text or focus'
        assert await b.js('__documentIdentity')==token
    await with_browser('online','',check,'window.__freshPolicy='+json.dumps(not cached_policy)+';')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
@pytest.mark.parametrize('cached_policy',[True,False])
def test_saved_login_popup_keeps_search_and_applies_fresh_instance_policy(cached_policy):
    asyncio.run(saved_popup(cached_policy))


async def delayed_native_apps():
    async def check(b):
        await b.until("!!document.querySelector('#os-q')")
        assert await b.js('!PCOSShell.available() && __waitingHTTP.length>0')
        await b.js("(()=>{const q=document.querySelector('#os-q');q.value='Firefox';q.focus();q.dispatchEvent(new Event('input',{bubbles:true}));q.setSelectionRange(1,4);window.__nativeSearch=q})()")
        assert await b.js('__appScans===0')
        await b.js('__releaseCompositor()')
        await b.until('PCOSShell.available()')
        # Wait for the host discovery call, not generic PosterChan menu row counts.
        for _ in range(20):
            if await b.js("!!document.querySelector('#os-startmenu [data-app=\"app:firefox-fixture\"]')"):break
            await asyncio.sleep(.05)
        assert await b.js("!!document.querySelector('#os-startmenu [data-app=\"app:firefox-fixture\"]')"),await b.js("({scans:__appScans,available:PCOSShell.available(),menu:document.querySelector('#os-startmenu').innerText})")
        assert await b.js('__instanceMode==="hang" && __waitingHTTP.length>0')
        assert await b.js("document.querySelector('#os-q')===__nativeSearch && __nativeSearch.value==='Firefox' && document.activeElement===__nativeSearch && __nativeSearch.selectionStart===1 && __nativeSearch.selectionEnd===4")
        assert await b.js("document.querySelector('#os-startmenu').textContent.includes('This computer')")
        assert await b.js("(()=>{const row=document.querySelector('#os-startmenu [data-app=\"app:firefox-fixture\"]');return row.getBoundingClientRect().height>0 && getComputedStyle(row).visibility==='visible' && row.innerText==='Firefox'})()")
        assert await b.js('__appScans===1')
        await b.js("document.querySelector('#os-startmenu [data-app=\"app:firefox-fixture\"]').click()")
        await b.until('__nativeActions.length===1')
        await asyncio.sleep(.1)
        assert await b.js('__nativeActions')==['app:app%3Afirefox-fixture']
    await with_browser('hang','?pcpopup=start',check,r'''
window.__appScans=0;window.__nativeActions=[];
const compositorReady=new Promise(resolve=>window.__releaseCompositor=()=>resolve([]));
window.pcWM={windows:()=>compositorReady};
window.pcApps={list:async()=>{__appScans++;return{apps:[{id:'firefox-fixture',name:'Firefox',comment:'Installed web browser'},{id:'btop-fixture',name:'btop',comment:'Installed system monitor'}]}}};
window.pcPopup.act=async action=>{__nativeActions.push(action);return true};
''')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_offline_start_discovers_delayed_native_apps_and_preserves_search():
    asyncio.run(delayed_native_apps())
