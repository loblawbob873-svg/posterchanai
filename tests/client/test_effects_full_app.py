"""Full client document and scripts: Social → Effects → upload/reply → original view.

Only HTTP and WebSocket boundaries are fixtures. Real login, signed Nostr event
parsing, DOM renderers, window manager and event handlers run unchanged. Every
outbound publish/upload is intercepted; Chrome also blocks external network URLs.
"""
import asyncio
import json
import threading
import tempfile
import subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from jinja2 import Template
import websockets
import httpx
import pytest

ROOT=Path(__file__).resolve().parents[2]


class Browser:
    def __init__(self, ws):self.ws,self.sequence=ws,0
    async def call(self,method,params=None):
        self.sequence+=1
        await self.ws.send(json.dumps({'id':self.sequence,'method':method,'params':params or {}}))
        while True:
            message=json.loads(await asyncio.wait_for(self.ws.recv(),15))
            if message.get('id')==self.sequence:
                assert 'error' not in message,message
                return message.get('result',{})
    async def js(self,expression):
        result=await self.call('Runtime.evaluate',{'expression':expression,'returnByValue':True,'awaitPromise':True})
        assert 'exceptionDetails' not in result,result
        return result['result'].get('value')

    async def until(self,expression):
        for _ in range(150):
            if await self.js(expression):return
            await asyncio.sleep(.1)
        raise AssertionError({'waiting':expression,'state':await self.js("({errors:__errors,body:document.body.innerText.slice(-2000),requests:__requests.slice(-20)})")})


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT),**kw)
    def log_message(self,*a):pass
    def do_GET(self):
        if self.path.split('?')[0]=='/client':
            data=Template((ROOT/'templates/client.html').read_text()).render(ver='test',build=1696,nostr_only=False,secure=True).encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html')
            self.end_headers()
            self.wfile.write(data)
        else:
            if self.path=='/fixture.png':self.path='/static/icon-192.png'
            if self.path.startswith('/client/') and self.path.split('?')[0].endswith('.js'): self.path='/static/js/client/'+self.path.removeprefix('/client/')
            super().do_GET()
INIT=r'''
window.__errors=[];onerror=(m)=>__errors.push(m);
window.__requests=[];window.__sockets=[];window.__published=[];window.__publishOK=false;window.__nextConversation=41;
const origFetch=window.fetch.bind(window);
window.fetch=async function(url,opts={}){
 const u=String(url);__requests.push([u,opts.method||'GET']);
 if(new URL(u,location.href).origin===location.origin && u.includes('/static/'))return origFetch(url,opts);
 let d={};
 if(u.includes('/client/config'))d={nostr_only:false,relay_url:'wss://fixture.invalid',relays:['wss://fixture.invalid']};
 if(u.includes('/auth/nostr-login'))d={access_token:'fixture-token',user:{id:1,can_ai:true,can_blossom:true,is_admin:true}};
 if(u.includes('/conversations'))d=opts.method==='POST'?{id:++__nextConversation,title:'Effects fixture'}:/conversations\/\d/.test(u)?{id:42,messages:[]}:(window.__hasChats?[{id:7,title:'Previous chat'}]:[]);
 if(u.includes('/client/effects'))d={effects:[{name:'glow',description:'Glow'}],motions:[],enhance:[]};
 if(u.includes('proxy-image')||u.includes('fixture.png'))return origFetch('/static/icon-192.png');
 if(u.includes('upload'))d={url:location.origin+'/fixture.png',sha256:'a'.repeat(64),size:100,type:'image/png'};
 return new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}});
};
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;__sockets.push(this);setTimeout(()=>{this.readyState=1;this.fire('open',{});},10)}
 fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e)}
 send(raw){const m=JSON.parse(raw);if(!Array.isArray(m))return;
 if(m[0]==='REQ')setTimeout(()=>{for(const ev of window.__events||[]){if(m.slice(2).some(f=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.ids||f.ids.includes(ev.id))&&(!f.authors||f.authors.includes(ev.pubkey))))this.fire('message',['EVENT',m[1],ev]);}this.fire('message',['EOSE',m[1]]);},15);
 if(m[0]==='EVENT'){__published.push(m[1]);setTimeout(()=>this.fire('message',['OK',m[1].id,__publishOK,'fixture reply']),15);}}
 close(){this.readyState=3;this.fire('close',{})}
}
window.WebSocket=FixtureSocket;
'''

async def main(width,existing_chat,native_window=False):
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-effects-full-') as profile:
        proc=subprocess.Popen(['/opt/google/chrome/chrome','--headless=new','--no-sandbox','--disable-gpu','--window-size=1440,1000','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile,'DevToolsActivePort').exists():break
                await asyncio.sleep(.1)
            port=Path(profile,'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h: pages=(await h.get('http://127.0.0.1:'+port+'/json')).json()
            async with websockets.connect(next(p for p in pages if p.get('type')=='page' and p.get('url')=='about:blank')['webSocketDebuggerUrl'],max_size=20_000_000) as ws:
                b=Browser(ws)
                await b.call('Page.enable')
                await b.call('Network.enable')
                await b.call('Network.setBlockedURLs',{'urls':['https://*','wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<600})
                await b.call('Page.addScriptToEvaluateOnNewDocument',{'source':'window.__hasChats='+json.dumps(existing_chat)+';'+INIT})
                await b.call('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/client'+('?pcwin=global' if native_window else '')})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                if native_window:assert await b.js('PCOSWin.isWindow()')
                await b.until("document.body.classList.contains('guest')")
                await b.js('''(()=>{const key=new Uint8Array(32).fill(1);window.__events=Array.from({length:12},(_,i)=>NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-i,content:'Effects test '+i+' '+location.origin+'/fixture.png',tags:[]},key));document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()''')
                await b.until("!!__PC.me() && document.querySelectorAll('.note').length>=12")
                if width > 600 and not native_window: await b.js("PCOS.enter();document.querySelector('.os-icon[data-view=global]').click()")
                else: await b.js("__PC.switchView('global')")
                await asyncio.sleep(1)
                await b.js("window.__sourceFeed=document.querySelector('#feed');__sourceFeed.scrollTop=250;window.__sourceNote=document.querySelector('.note');__sourceNote.querySelector('[data-a=menu]').click()")
                await b.js("window.__sourceTop=__sourceFeed.scrollTop;window.__sourceCardTop=__sourceNote.getBoundingClientRect().top-__sourceFeed.getBoundingClientRect().top;document.querySelector('.menu-pop [data-m=effect]').click()")
                await asyncio.sleep(2)
                await b.until("!!document.querySelector('#fxs-close')")
                await b.js("document.querySelector('#fxs-close').click()")
                await b.js("window.__aiWindow=document.querySelector('.ai-chat').closest('.osw');window.__aiNode=document.querySelector('.ai-chat');window.__aiComposer=document.querySelector('#ai-input')")
                async def check_geometry(stage):
                    geometry=await b.js("""(()=>{const chat=__aiNode,compose=chat.querySelector('.ai-compose'),feed=chat.parentElement,body=feed.closest('.osw-body')||feed;const rect=e=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height,cls:e.className}};return {chat:rect(chat),compose:rect(compose),input:rect(__aiComposer),send:rect(chat.querySelector('#ai-send')),feed:rect(feed),body:rect(body),padding:getComputedStyle(compose).paddingBottom}})()""")
                    assert abs(geometry['body']['bottom']-geometry['compose']['bottom'])<25,geometry
                    if width>820 or native_window:
                        assert abs(geometry['body']['bottom']-geometry['send']['bottom'])<30,(stage,geometry)
                    else:
                        assert float(geometry['padding'].removesuffix('px'))>=62, 'phone navigation clearance must remain'
                await check_geometry('initial')
                if width>820 and not native_window:
                    await b.js("__aiWindow.querySelector('[data-w=max]').click()")
                    await asyncio.sleep(.3)
                    await check_geometry('maximized')
                    await b.js("__aiComposer.value='Preserve draft';document.querySelector('.os-icon[data-view=notes]').click()")
                    await asyncio.sleep(.2)
                    await check_geometry('parked')
                    await b.js("__aiWindow.querySelector('.osw-bar').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true}))")
                    await asyncio.sleep(.2)
                    await check_geometry('restored')
                    assert await b.js("document.querySelector('#ai-input')===__aiComposer && __aiComposer.value==='Preserve draft'")
                assert await b.js("(()=>{const b=document.querySelector('#ai-back-social'),r=b.getBoundingClientRect(),bar=b.closest('.ai-bar');return !b.hidden&&r.width>60&&r.left>=0&&r.right<=innerWidth&&bar.scrollWidth<=bar.clientWidth+1})()")
                await b.js("__sockets.findLast(s=>s.url.includes('/api/ws/chat/')).fire('message',{type:'response',data:{type:'generated_image',image:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1sAAAAASUVORK5CYII='}})")
                await b.until("!!document.querySelector('.ai-reply-fx')")
                await b.js("document.querySelector('.ai-reply-fx').click()")
                await b.until("__published.some(e=>e.kind===1) && !document.querySelector('.ai-reply-fx').disabled")
                assert await b.js("__PC.isView('ai') && !document.querySelector('.ai-reply-fx').disabled && !document.querySelector('#ai-back-social').hidden")
                await b.js("__publishOK=true;document.querySelector('.ai-reply-fx').click()")
                await b.until("__PC.isView('global')")
                await asyncio.sleep(.5)
                assert await b.js("document.querySelector('#feed')===__sourceFeed")
                if width>600 and not native_window:assert await b.js("__sourceNote.isConnected")
                if width>600 and not native_window:
                    assert await b.js("Math.abs(__sourceFeed.scrollTop-__sourceTop)<2")
                else:
                    # Newly published rows and decoded images can change pixel offsets.
                    # Keep the same first visible post as the reader's position.
                    assert await b.js("[...__sourceFeed.querySelectorAll('.note')].find(n=>n.getBoundingClientRect().bottom>__sourceFeed.getBoundingClientRect().top)?.dataset.id===__sourceNote.dataset.id")
                assert await b.js("__published.filter(e=>e.kind===1).every(e=>e.tags.some(t=>t[0]==='e'&&t[1]===__sourceNote.dataset.id))")
                assert not await b.js('__errors')

        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
            server.server_close()
@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome is required')
@pytest.mark.parametrize('width',[1440,390])
@pytest.mark.parametrize('existing_chat',[False,True])
def test_full_app_effects(width,existing_chat):
    asyncio.run(main(width,existing_chat))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome is required')
@pytest.mark.parametrize('width', [1440, 780])
def test_native_window_bootstrap_effects_return(width):
    # Real pcwin URL/adoption/route guards; no Electron compositor or shared opener.
    asyncio.run(main(width,False,native_window=True))
