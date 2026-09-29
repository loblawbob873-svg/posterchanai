"""The real bundled client boots with musiclib.js split out of app.js, and the Music library works.

Drives the SHIPPED desktop bundle (desktop/build-www.sh renders templates/client.html and copies
every client script) in headless Chrome, through the same harness as the offline desktop test. The
drive is stubbed at the network boundary only: a track is encrypted by the app's OWN master-key code
(`__PC.driveEnc` -> the forwarded _masterEncrypt), its ciphertext is what the stubbed media server
serves, and everything after that is the shipped code. What a broken split looks like:

  * musiclib.js loaded after app.js (or missing) -> the factory is built later, by whichever call
    reaches a forwarder first (or fetched again by the lazy loader) — caught by WHERE it was built:
    once, during app.js's own evaluation, from `_musiclibMod`, never from `_lzRun`;
  * a dependency not passed, or a live binding captured by value -> the library does not list, or
    trackUrl throws (`_blobHave`'s setter missing would leave the track marked missing);
  * the MusicOffline proxy not reaching the module's object -> its methods throw;
  * and no page error or console error anywhere.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BUILD_PROBE = r'''
window.__consoleErrors=[];
{const ce=console.error.bind(console);console.error=(...a)=>{__consoleErrors.push(a.map(x=>String(x&&x.stack||x)).join(' ').slice(0,400));ce(...a);};}
addEventListener('unhandledrejection',e=>__consoleErrors.push('unhandled: '+String(e.reason&&e.reason.stack||e.reason).slice(0,400)));
{let real;Object.defineProperty(window,'PCMusicLibFactory',{configurable:true,get(){return real;},set(f){
  real=function(dep){
    window.__mlBuilds=(window.__mlBuilds||0)+1;
    window.__mlBuiltIn=(document.currentScript&&document.currentScript.src||'').split('?')[0].split('/').pop();
    window.__mlBuiltStack=String(new Error().stack);
    window.__mlBuiltBeforePC=!window.__PC;
    return f.apply(this,arguments);
  };}});}
// The stubbed media server: GET /<sha> answers the ciphertext the test registers, /list/<pk> lists it.
window.__blobs={};
{const prev=window.fetch;window.fetch=function(url,opts){
  const u=String(url), m=u.match(/\/([0-9a-f]{64})(?:[?#].*)?$/);
  // The network fixture answers every non-/static fetch with JSON, blob: URLs included — read an
  // object URL (what trackUrl returns) the way the browser would.
  if(u.startsWith('blob:')) return new Promise((ok,no)=>{const x=new XMLHttpRequest();x.open('GET',u);x.responseType='arraybuffer';
    x.onload=()=>ok(new Response(x.response,{status:200}));x.onerror=()=>no(new TypeError('blob read failed'));x.send();});
  if(m && __blobs[m[1]]) return Promise.resolve(new Response(__blobs[m[1]],{status:200,headers:{'Content-Type':'application/octet-stream'}}));
  if(/\/list\/[0-9a-f]{64}/.test(u)) return Promise.resolve(new Response(JSON.stringify(Object.keys(__blobs).map(s=>({sha256:s,size:__blobs[s].length}))),{status:200,headers:{'Content-Type':'application/json'}}));
  return prev.apply(this,arguments);
};}
'''


async def _boot_and_play(b):
    order = await b.js("[...document.scripts].map(s=>(s.getAttribute('src')||'').split('?')[0].split('/').pop())"
                       ".filter(n=>n==='musiclib.js'||n==='app.js')")
    assert order == ['musiclib.js', 'app.js'], order
    assert await b.js("window.__mlBuilds") == 1
    assert await b.js("window.__mlBuiltIn") == 'app.js'
    assert await b.js("window.__mlBuiltBeforePC") is True
    stack = await b.js("window.__mlBuiltStack")
    assert '_musiclibMod' in stack and '_lzRun' not in stack, stack

    await desktop.login(b)

    # A track encrypted by the app's own master-key path, registered with the stubbed media server and
    # added to the drive index as a Music entry.
    sha = await b.js("""(async()=>{
      const plain=new TextEncoder().encode('OggS fixture track bytes '+'x'.repeat(200));
      const ct=new Uint8Array(await __PC.driveEnc(plain));
      const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',ct))].map(x=>x.toString(16).padStart(2,'0')).join('');
      __blobs[sha]=ct; window.__plain=plain;
      await __PC.musicLibraryAdd([[sha,{name:'Fixture song',folder:'Music',mime:'audio/ogg',enc:true,mk:true,
        size:ct.length,ts:Math.floor(Date.now()/1000)}]]);
      return sha;})()""")
    assert len(sha) == 64
    assert sha in await b.js("__PC.musicLibrary()")

    # trackUrl resolves it: fetched from the stubbed server, decrypted, handed back as an object URL.
    same = await b.js(f"""(async()=>{{const got=await __PC.musicPlainOf('{sha}');
      return got.length===__plain.length && got.every((v,i)=>v===__plain[i]) ? true : [got.length, __plain.length, new TextDecoder().decode(got.slice(0,60))];}})()""")
    assert same is True, same

    # MusicOffline through app.js's Proxy reaches the module's real object.
    assert await b.js("typeof __PC.MusicOffline.budgetBytes()==='number'")
    assert await b.js("__PC.MusicOffline.usage().then(u=>!!u&&typeof u==='object')")

    # The library LISTS it: the Music app paints the track by name (musicEntries/musicTracks, with
    # _refreshBlobHave writing app.js's _blobHave through the setter — a stale copy marks it missing).
    await b.js("__PC.switchView('music')")
    await b.until("(document.querySelector('#feed')||{}).innerText && document.querySelector('#feed').innerText.includes('Fixture song')")

    fetched = await b.js("performance.getEntriesByType('resource').filter(e=>/\\/musiclib\\.js(\\?|$)/.test(e.name)).length")
    assert fetched == 1, fetched
    assert await b.js("window.__mlBuilds") == 1
    assert not await b.js('__errors'), await b.js('__errors')
    assert not await b.js('__consoleErrors'), await b.js('__consoleErrors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_bundled_client_boots_with_musiclib_js_and_the_library_plays():
    asyncio.run(desktop.with_browser('online', '', _boot_and_play, extra_init=BUILD_PROBE))
