"""Real client login/composer/Drafts/relay/outbox with only HTTP and socket fixtures.

The relay opens successfully but never sends a frame; retries must redial it. No
application functions or timers are replaced and no public network write escapes.
Set PC_DRAFTS_APP_ROOT to validate an isolated implementation checkout.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
import httpx
import pytest
import websockets
from tests.client import test_effects_full_app as full

APP_ROOT=Path(os.environ.get('PC_DRAFTS_APP_ROOT',Path(__file__).resolve().parents[2]))
SOCKET=r'''
window.__fixtureMode=localStorage.getItem('__fixtureMode')||'silent';
window.__pendingAcks=[];
// Observe actual worker sign requests without replacing signer behavior or capturing keys.
const workerPostMessage=Worker.prototype.postMessage;
Worker.prototype.postMessage=function(message,...rest){
 if(message&&message.op==='sign'&&message.args?.event?.kind===1)
  localStorage.setItem('__fixtureNoteSigns',String(Number(localStorage.getItem('__fixtureNoteSigns')||0)+1));
 return workerPostMessage.call(this,message,...rest);
};
const fixtureFetch=window.fetch;
window.fetch=async function(url,opts={}){
 if(String(url).includes('/client/drafts')){
  const body=JSON.parse(opts.body||'{}');
  return new Response(JSON.stringify({ok:true,drafts:body.drafts||JSON.parse(localStorage.getItem('__fixtureRemoteDrafts')||'[]')}),{status:200,headers:{'Content-Type':'application/json'}});
 }
 return fixtureFetch(url,opts);
};
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;this.deaf=__fixtureMode==='silent';__sockets.push(this);setTimeout(()=>{this.readyState=1;this.fire('open',{});},10)}
 fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e)}
 send(raw){const m=JSON.parse(raw);if(!Array.isArray(m))return;
  if(m[0]==='EVENT')__published.push(m[1]);
  if(this.deaf)return;
  if(m[0]==='REQ')setTimeout(()=>this.fire('message',['EOSE',m[1]]),15);
  if(m[0]==='EVENT'){
   if(m[1].kind===1){__pendingAcks.push({socket:this,event:m[1]});if(__fixtureMode==='reject')setTimeout(()=>this.fire('message',['OK',m[1].id,false,'blocked: fixture rejection']),15);}
   else setTimeout(()=>this.fire('message',['OK',m[1].id,true,'fixture accepted']),15);
  }
 }
 close(){this.readyState=3;this.fire('close',{})}
}
window.WebSocket=FixtureSocket;
window.__liveDrafts=()=>JSON.parse(localStorage.getItem('pc_drafts_'+__PC.me().pubkey)||'[]').filter(d=>!d.del&&d.text);
window.__queued=()=>JSON.parse(localStorage.getItem('pc_outbox')||'[]');
'''
INIT=full.INIT.split('class FixtureSocket')[0]+SOCKET


async def run_drafts():
    full.ROOT=APP_ROOT
    server=ThreadingHTTPServer(('127.0.0.1',0),full.Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-drafts-full-') as profile:
        proc=subprocess.Popen(['/opt/google/chrome/chrome','--headless=new','--no-sandbox','--disable-gpu','--window-size=1440,1000','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile,'DevToolsActivePort').exists():break
                await asyncio.sleep(.1)
            port=Path(profile,'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:pages=(await h.get(f'http://127.0.0.1:{port}/json')).json()
            async with websockets.connect(next(p for p in pages if p.get('type')=='page')['webSocketDebuggerUrl'],max_size=20_000_000) as ws:
                b=full.Browser(ws)
                await b.call('Page.enable');await b.call('Network.enable')
                await b.call('Network.setBlockedURLs',{'urls':['https://*','wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':900,'deviceScaleFactor':1,'mobile':True})
                await b.call('Page.addScriptToEvaluateOnNewDocument',{'source':INIT})
                await b.call('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/client'})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')
                assert await b.js('__sockets.some(s=>s.readyState===1&&s.deaf)')
                await b.js("document.querySelector('#btn-compose-m').click()")
                await b.until("!!document.querySelector('#cmp-send')")
                await b.js("(()=>{const ta=document.querySelector('#cmp');ta.value='Offline draft browser fixture';ta.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-send').click()})()")
                await b.until('__queued().length===1 && __liveDrafts().length===1')
                original=await b.js('__queued()[0].ev')
                assert original['content']=='Offline draft browser fixture'
                assert await b.js("Number(localStorage.getItem('__fixtureNoteSigns'))===1"), 'initial UI post did not sign exactly once'
                assert await b.js('NostrTools.verifyEvent(__queued()[0].ev)')
                await b.js("localStorage.setItem('__fixtureRemoteDrafts',JSON.stringify(__liveDrafts()));__PC.switchView('drafts')")
                await b.until("!!document.querySelector('.draft-card [data-act=send]')")
                # A recovered connection explicitly rejects the retry. It must keep the only
                # editable copy and reuse the queued signature, including across a page reload.
                await b.js("__fixtureMode='reject';localStorage.setItem('__fixtureMode','reject');document.querySelector('.draft-card [data-act=send]').click()")
                await asyncio.sleep(.2)
                assert await b.js("Number(localStorage.getItem('__fixtureNoteSigns'))===1"), 'Draft Send issued an extra kind-1 signer request'
                await b.until('__pendingAcks.length>0')
                await b.until("!!document.querySelector('.draft-card [data-act=send]:not([disabled])')")
                assert await b.js('__liveDrafts().length===1')
                assert await b.js('__sockets.some(s=>s.deaf&&s.readyState===3)'), 'silent OPEN relay was not replaced'
                sent=await b.js('__published.filter(e=>e.kind===1)')
                assert sent and all(e==original for e in sent), 'Draft Send signed another event instead of retrying'
                await b.call('Page.reload',{'ignoreCache':True})
                await b.until('!!window.__PC && !!__PC.me()')
                await b.js("__PC.switchView('drafts')")
                await b.until("!!document.querySelector('.draft-card [data-act=send]')")
                assert await b.js('__liveDrafts().length===1')
                # Hold all positive acknowledgements. Merely sending bytes or receiving EOSE
                # must not remove the draft. Only the configured relay's OK may settle it.
                await b.js("__fixtureMode='hold';localStorage.setItem('__fixtureMode','hold');__pendingAcks=[];document.querySelector('.draft-card [data-act=send]').click()")
                await b.until('__pendingAcks.length>0')
                await asyncio.sleep(.2)
                assert await b.js('__liveDrafts().length===1'), 'draft removed before trusted ACK'
                sent=await b.js('__published.filter(e=>e.kind===1)')
                assert all(e==original for e in sent), 'reload/retry changed the signed event'
                assert await b.js("Number(localStorage.getItem('__fixtureNoteSigns'))===1"), 'reload/retry signed again'
                await b.js("__pendingAcks.forEach(x=>x.socket.fire('message',['OK','f'.repeat(64),true,'wrong event']))")
                await asyncio.sleep(.1)
                assert await b.js('__liveDrafts().length===1'), 'unmatched ACK removed the draft'
                await b.js("__pendingAcks.filter(x=>x.socket.url.includes('fixture.invalid')).forEach(x=>x.socket.fire('message',['OK',x.event.id,true,'stored']))")
                await b.until('__liveDrafts().length===0 && __queued().length===0')
                await b.until("!document.querySelector('.draft-card')")
                # The HTTP fixture deliberately serves the pre-delivery draft after reload,
                # exercising the real tombstone merge against an old server replica.
                await b.call('Page.reload',{'ignoreCache':True})
                await b.until('!!window.__PC && !!__PC.me()')
                await b.js("__PC.switchView('drafts')")
                await b.until("!!document.querySelector('#drafts-empty')")
                await asyncio.sleep(2)
                assert await b.js('__liveDrafts().length===0 && __queued().length===0'), 'accepted draft resurrected on reload'
                assert not await b.js('__errors')
        finally:
            proc.terminate();proc.wait(timeout=10)
            server.shutdown();server.server_close()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome is required')
def test_draft_delivery_survives_silent_open_relay_and_reload():
    asyncio.run(run_drafts())
