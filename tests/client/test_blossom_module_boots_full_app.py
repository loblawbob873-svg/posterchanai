"""The real bundled client boots with blossom.js split out of app.js, and a pasted/dropped file uploads.

Drives the SHIPPED desktop bundle (desktop/build-www.sh renders templates/client.html and copies
every client script) in headless Chrome, through the same harness as the offline desktop test. The
instance is stubbed at the network boundary only: /client/config says the built-in Blossom server is
on, /client/blossom-access says this account may use it, and `PUT <server>/upload` answers with a
blob URL. Everything between — the composer's paste and drop handlers, upload.js's uploadBlob, and
the moved code that decides WHERE it goes (checkBlossomAccess, uploadTarget, _blossomBuiltin,
_serverOrigin, mediaServer) — is the shipped code. What a broken split looks like:

  * blossom.js loaded after app.js (or missing) -> the factory is built later, by whichever call
    reaches a forwarder first (or fetched again by the lazy loader), and its top-level statements —
    the media-fetch ceiling around window.fetch, the focus/online/storage listeners — run at the
    wrong moment or never: caught by WHERE it was built (once, during app.js's own evaluation, from
    `_blossomMod`, never from `_lzRun`) and by the ceiling having been installed by then;
  * a dependency not passed, or a live binding captured by value (`S._blossomOK` without its setter
    leaves the permission unknown for ever) -> the upload goes to the wrong server, or throws;
  * and no page error or console error anywhere.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BUILD_PROBE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__consoleErrors=[];
{const ce=console.error.bind(console);console.error=(...a)=>{__consoleErrors.push(a.map(x=>String(x&&x.stack||x)).join(' ').slice(0,400));ce(...a);};}
addEventListener('unhandledrejection',e=>__consoleErrors.push('unhandled: '+String(e.reason&&e.reason.stack||e.reason).slice(0,400)));
{let real;Object.defineProperty(window,'PCBlossomFactory',{configurable:true,get(){return real;},set(f){
  real=function(dep){
    window.__blBuilds=(window.__blBuilds||0)+1;
    window.__blBuiltIn=(document.currentScript&&document.currentScript.src||'').split('?')[0].split('/').pop();
    window.__blBuiltStack=String(new Error().stack);
    window.__blBuiltBeforePC=!window.__PC;
    window.__blFetchBoundBefore=!!window.__pcMediaFetchBound;
    const out=f.apply(this,arguments);
    window.__blFetchBoundAfter=!!window.__pcMediaFetchBound;   // the block's top-level IIFE ran in the build
    return out;
  };}});}
// The instance, at the network boundary: built-in Blossom on, this account allowed, PUT /upload answers.
window.__uploads=[];
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  const u=String(url), method=String(opts.method||'GET').toUpperCase();
  if(u.includes('/client/config')){const r=await prev.apply(this,arguments);const d=await r.json();
    return new Response(JSON.stringify({...d,blossom_enabled:true}),{status:200,headers:{'Content-Type':'application/json'}});}
  if(u.includes('/client/blossom-access'))return new Response(JSON.stringify({allowed:true,whitelisted:true}),{status:200,headers:{'Content-Type':'application/json'}});
  if(method==='PUT'&&/\/upload$/.test(u)){
    const auth=JSON.parse(atob(String((opts.headers||{}).Authorization||'').replace(/^Nostr /,'')));
    const sha=(__uploads.length+1).toString(16).padStart(64,'0');
    __uploads.push({url:u,kind:auth.kind,size:(opts.body&&opts.body.size)||0});
    return new Response(JSON.stringify({url:u.replace(/\/upload$/,'/')+sha+'.png',sha256:sha}),{status:200,headers:{'Content-Type':'application/json'}});}
  return prev.apply(this,arguments);
};}
window.__png=()=>{const b=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlZkAAAAASUVORK5CYII='),c=>c.charCodeAt(0));
  const d=new DataTransfer();d.items.add(new File([b],'pasted.png',{type:'image/png'}));return d;};
'''


async def _boot_and_upload(b):
    order = await b.js("[...document.scripts].map(s=>(s.getAttribute('src')||'').split('?')[0].split('/').pop())"
                       ".filter(n=>n==='blossom.js'||n==='app.js')")
    assert order == ['blossom.js', 'app.js'], order
    assert await b.js("window.__blBuilds") == 1
    assert await b.js("window.__blBuiltIn") == 'app.js'
    assert await b.js("window.__blBuiltBeforePC") is True
    stack = await b.js("window.__blBuiltStack")
    assert '_blossomMod' in stack and '_lzRun' not in stack, stack
    assert await b.js("window.__blFetchBoundBefore") is False
    assert await b.js("window.__blFetchBoundAfter") is True

    await desktop.login(b)
    server = await b.js("__PC.mediaServer ? __PC.mediaServer() : null")

    # PASTE: an image pasted into the composer is uploaded and its URL lands in the text.
    await b.js("__PC.compose()")
    await b.until("!!document.querySelector('#cmp')")
    await b.js("document.querySelector('#cmp').dispatchEvent(new ClipboardEvent('paste',"
               "{clipboardData:__png(),bubbles:true,cancelable:true}))")
    first = '0' * 63 + '1'
    await b.until(f"document.querySelector('#cmp').value.includes({json.dumps(first)})")

    # DROP: a file dropped on the composer takes the same route.
    await b.js("(()=>{const ta=document.querySelector('#cmp');const root=ta.closest('.modal')||ta.parentElement;"
               "root.dispatchEvent(new DragEvent('drop',{dataTransfer:__png(),bubbles:true,cancelable:true}));})()")
    second = '0' * 63 + '2'
    await b.until(f"document.querySelector('#cmp').value.includes({json.dumps(second)})")

    uploads = await b.js("__uploads")
    assert len(uploads) == 2, uploads
    for up in uploads:
        # BUD-02 to the BUILT-IN server (checkBlossomAccess -> uploadTarget -> _blossomBuiltin), signed
        # with a kind-24242 upload authorisation — not diverted to nostr.build's NIP-96.
        assert up['url'].endswith('/blossom/upload'), up
        assert up['kind'] == 24242, up
        if server:
            assert up['url'] == server + '/upload', (server, up)
    value = await b.js("document.querySelector('#cmp').value")
    assert value.count('/blossom/') == 2, value

    fetched = await b.js("performance.getEntriesByType('resource').filter(e=>/\\/blossom\\.js(\\?|$)/.test(e.name)).length")
    assert fetched == 1, fetched
    assert await b.js("window.__blBuilds") == 1
    assert not await b.js('__errors'), await b.js('__errors')
    assert not await b.js('__consoleErrors'), await b.js('__consoleErrors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_bundled_client_boots_with_blossom_js_and_a_pasted_file_uploads():
    asyncio.run(desktop.with_browser('online', '', _boot_and_upload, extra_init=BUILD_PROBE))
