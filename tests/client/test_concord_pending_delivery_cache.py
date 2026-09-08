"""Real IndexedDB quota pressure and reload for the protected encrypted delivery journal."""
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading
import httpx
import pytest
import websockets
from tests.client.test_effects_full_app import Browser

ROOT=Path(__file__).resolve().parents[2]
PAGE=r'''<!doctype html><script src="cache.js"></script><script>
(async()=>{try{
 const C=PCConcordCache,scope=JSON.stringify(['room','owner:channel']),history='room-history';
 const event=i=>({id:'pending-'+i,kind:1059,content:'encrypted-'+i,created_at:15000+i,tags:[],plaintext:'never persist me'});
 if(sessionStorage.getItem('phase')){
   const pending=await C.getDeliveries(scope);
   if(pending.some(e=>e.id==='pending-0'))throw Error('ACKed entry resurrected');
   if(pending.length!==64)throw Error('pending entries lost on reload');
   if(JSON.stringify(pending).includes('never persist me'))throw Error('plaintext persisted');
   const foreign=await C.getDeliveries(JSON.stringify(['room','other-owner:channel']));
   if(foreign.length)throw Error('account scopes mixed');
   window.__result={ok:true,count:pending.length,pressure:true,atomicAdmission:true,reload:true};return;
 }
 await C.putDelivery(scope,event(0));
 const pressure=Array.from({length:600},(_,i)=>({id:'history-'+i,kind:1059,created_at:10000+i,content:'x'.repeat(60000),tags:[]}));
 await C.put('history-pressure',pressure);
 if((await C.getDeliveries(scope)).length!==1)throw Error('history pressure evicted pending');
 for(let i=1;i<64;i++)await C.putDelivery(scope,event(i));
 let refused=false;try{await C.putDelivery(scope,event(64));}catch(_){refused=true;}
 if(!refused)throw Error('pending quota did not fail closed');
 await C.putDelivery(scope,event(0)); // identical retry admission must remain idempotent at quota
 await C.completeDelivery(scope,'pending-0',history);
 if(!(await C.get(history)).some(e=>e.id==='pending-0'))throw Error('ACK did not atomically retain canonical ciphertext');
 if((await C.getDeliveries(scope)).length!==63)throw Error('ACK left pending entry');
 const race=await Promise.allSettled([C.putDelivery(scope,event(64)),C.putDelivery(scope,event(65))]);
 if(race.filter(r=>r.status==='fulfilled').length!==1)throw Error('concurrent callers bypassed quota');
 await C.put('history-pressure-2',pressure.map(e=>({...e,id:e.id+'-again',created_at:e.created_at+10000})));
 if((await C.get(history)).length)throw Error('history pressure fixture did not evict canonical ACK history');
 sessionStorage.setItem('phase','reload');location.reload();
}catch(e){window.__result={error:String(e.stack||e)};}})();
</script>'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_pending_delivery_survives_history_eviction_and_ack_does_not_resurrect(tmp_path):
    (tmp_path/'index.html').write_text(PAGE)
    shutil.copyfile(ROOT/'static/js/client/concord-cache.js',tmp_path/'cache.js')
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    profile=tmp_path/'profile'
    proc=subprocess.Popen(['/opt/google/chrome/chrome','--headless=new','--no-sandbox','--disable-gpu','--remote-debugging-port=0','--user-data-dir='+str(profile),'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    async def run():
        for _ in range(100):
            if (profile/'DevToolsActivePort').exists():break
            await asyncio.sleep(.1)
        port=(profile/'DevToolsActivePort').read_text().splitlines()[0]
        async with httpx.AsyncClient() as http:pages=(await http.get(f'http://127.0.0.1:{port}/json')).json()
        async with websockets.connect(next(p for p in pages if p.get('type')=='page')['webSocketDebuggerUrl']) as ws:
            browser=Browser(ws)
            await browser.call('Page.enable')
            await browser.call('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/index.html'})
            for _ in range(150):
                result=await browser.js('window.__result||null')
                if result:return result
                await asyncio.sleep(.1)
            raise AssertionError('IndexedDB delivery scenario did not complete')
    try:
        result=asyncio.run(run())
        assert result.get('ok'),result
    finally:
        proc.terminate();proc.wait(timeout=10);server.shutdown();server.server_close()
