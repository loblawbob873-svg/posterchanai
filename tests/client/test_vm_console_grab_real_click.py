"""The VM console must GRAB the mouse when somebody clicks its screen — measured with a REAL click in
headless Chrome against the shipped vmconsole.js and the vendored noVNC, never against a stub RFB.

Why a real click and a real noVNC: the first version (adaa1eade) listened for `click` on the screen
container, and it could never fire. noVNC's canvas handles `click` itself and calls
`stopPropagation()` (rfb.js `_handleMouse`), and on `mousedown` it shows a full-page capture element
(`setCapture` → #noVNC_mouse_capture_elem) so the `mouseup` lands on THAT, making the click's target
the common ancestor (<body>) — not the screen. Either one alone is enough to starve the listener; a
string test ("requestPointerLock" appears in the file) passed while the grab was unreachable. The user
reported it as "need Cursor Lock ... Still an issue".

So this drives trusted CDP mouse input at the canvas and asserts, in order:
  * a click on the screen REQUESTS pointer lock (the grab);
  * once locked, a huge relative move is CLAMPED to the VM's framebuffer — the VNC PointerEvent the
    server receives never leaves 0..width-1 / 0..height-1 — i.e. the cursor cannot leave the screen;
  * clicks while locked still reach the VM (a button-down pointer event is sent);
  * releasing the lock stops the interception (a plain move is no longer rewritten);
  * the toolbar button (vms.js) and the Esc hint are wired — the grab is discoverable, not a secret.
"""
import asyncio
import tempfile
import threading
import subprocess
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler

ROOT = Path(__file__).resolve().parents[2]

PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#111}
#screen{position:absolute;left:100px;top:80px;width:640px;height:480px;overflow:hidden}
</style></head><body><div id="screen"></div>
<script>
window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message||e)));
window.__ptr=[];window.__lockReq=0;window.__status=[];
// Headless Chrome has no window-manager focus to lock against, so the lock is SIMULATED at the one
// seam the browser owns: requestPointerLock records the call and makes the element the lock target.
let __locked=null;
Object.defineProperty(Document.prototype,'pointerLockElement',{configurable:true,get(){return __locked;}});
Element.prototype.requestPointerLock=function(){__lockReq++;__locked=this;setTimeout(()=>document.dispatchEvent(new Event('pointerlockchange')),0);return Promise.resolve();};
Document.prototype.exitPointerLock=function(){__locked=null;setTimeout(()=>document.dispatchEvent(new Event('pointerlockchange')),0);};
function u8(...p){const n=p.reduce((a,x)=>a+x.length,0),o=new Uint8Array(n);let i=0;for(const x of p){o.set(x,i);i+=x.length;}return o;}
const be16=n=>[n>>8&255,n&255],be32=n=>[n>>>24&255,n>>16&255,n>>8&255,n&255];
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=url;this.readyState=0;this.protocol='';this.binaryType='blob';this.rfb=null;this.rx=new Uint8Array(0);
   setTimeout(()=>{this.readyState=1;this.emit('open',{});},10);}
 emit(t,init){const e=t==='message'?new MessageEvent('message',init):new Event(t);this['on'+t]?.(e);this.dispatchEvent(e);}
 text(o){setTimeout(()=>this.emit('message',{data:JSON.stringify(o)}),5);}
 bin(b){const x=b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength);setTimeout(()=>this.emit('message',{data:x}),5);}
 send(raw){
   if(typeof raw==='string'){const m=JSON.parse(raw);if(m.t==='open')return this.text({t:'ok'});if(m.t==='go'){this.rfb='version';this.bin(new TextEncoder().encode('RFB 003.008\n'));}return;}
   const c=new Uint8Array(raw instanceof ArrayBuffer?raw:raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength));
   this.rx=u8(this.rx,c);
   for(;;){const b=this.rx;
     if(this.rfb==='version'){if(b.length<12)return;this.rx=b.slice(12);this.rfb='sec';this.bin(new Uint8Array([1,1]));continue;}
     if(this.rfb==='sec'){if(b.length<1)return;this.rx=b.slice(1);this.rfb='init';this.bin(new Uint8Array(be32(0)));continue;}
     if(this.rfb==='init'){if(b.length<1)return;this.rx=b.slice(1);this.rfb='normal';const nm=new TextEncoder().encode('fx');
       this.bin(new Uint8Array([...be16(64),...be16(48),32,24,0,1,...be16(255),...be16(255),...be16(255),16,8,0,0,0,0,...be32(nm.length),...nm]));continue;}
     if(this.rfb!=='normal'||!b.length)return;
     const t=b[0];let len=0;
     if(t===0)len=20;else if(t===2){if(b.length<4)return;len=4+4*((b[2]<<8)|b[3]);}
     else if(t===3)len=10;else if(t===4)len=8;else if(t===5)len=6;
     else if(t===6){if(b.length<8)return;len=8+((b[4]<<24)|(b[5]<<16)|(b[6]<<8)|b[7]);}
     else if(t===150)len=10;else{this.rx=new Uint8Array(0);return;}
     if(b.length<len)return;
     if(t===5)__ptr.push({mask:b[1],x:(b[2]<<8)|b[3],y:(b[4]<<8)|b[5]});
     this.rx=b.slice(len);
     if(t===3&&!this.sent){this.sent=true;const px=new Uint8Array(64*48*4).fill(200);
       this.bin(u8(new Uint8Array([0,0,...be16(1),...be16(0),...be16(0),...be16(64),...be16(48),...be32(0)]),px));}
   }
 }
 close(){this.readyState=3;this.emit('close',{code:1000,wasClean:true});}
}
</script>
<script src="/static/js/client/vmconsole.js"></script>
<script>
(async()=>{window.__h=await PCVmConsole.open({target:document.getElementById('screen'),url:'ws://fixture.invalid/ws/vmconsole',
  ticket:'t',password:'p',WebSocket:FixtureSocket,onStatus:(s,m)=>{window.__state=s;__status.push(m);}});})();
</script></body></html>'''


class PageHandler(Handler):
    def do_GET(self):
        if self.path.split('?')[0] == '/vmc-fixture':
            data = PAGE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()


async def mouse(b, kind, x, y, button='none', buttons=0):
    await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': button,
                                             'buttons': buttons, 'clickCount': 1 if kind != 'mouseMoved' else 0})


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), PageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-vmc-grab-') as profile:
        proc = subprocess.Popen(['/opt/google/chrome/chrome', '--headless=new', '--no-sandbox', '--disable-gpu',
                                 '--window-size=1000,700', '--remote-debugging-port=0', '--user-data-dir=' + profile,
                                 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, 'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get('http://127.0.0.1:' + port + '/json')).json()
            page = next(p for p in pages if p.get('type') == 'page')
            async with websockets.connect(page['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call('Page.enable')
                await b.call('Emulation.setDeviceMetricsOverride', {'width': 1000, 'height': 700, 'deviceScaleFactor': 1, 'mobile': False})
                await b.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/vmc-fixture'})
                for _ in range(100):
                    if await b.js("window.__state==='connected' && !!document.querySelector('#screen canvas')"):
                        break
                    await asyncio.sleep(.1)
                assert await b.js("window.__state==='connected'"), await b.js("JSON.stringify({s:window.__state,st:__status,e:__errors})")
                r = await b.js("(()=>{const r=document.querySelector('#screen canvas').getBoundingClientRect();return [r.left,r.top,r.width,r.height];})()")
                cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
                # ---- 1. a REAL click on the VM's screen grabs the mouse
                await mouse(b, 'mouseMoved', cx, cy)
                await mouse(b, 'mousePressed', cx, cy, 'left', 1)
                await mouse(b, 'mouseReleased', cx, cy, 'left', 0)
                await asyncio.sleep(.2)
                assert await b.js("__lockReq") >= 1, 'a click on the console screen must request pointer lock'
                assert await b.js("!!document.pointerLockElement"), 'the lock took'
                assert await b.js("document.getElementById('screen').classList.contains('vmc-grabbed')")
                assert await b.js("__status.some(m=>/Esc/.test(m))"), 'while grabbed the status says how to get out'
                # ---- 2. while locked a huge move cannot leave the framebuffer (64x48). The real pointer
                # is locked, so what reaches the page is movementX/Y; a move across the whole 1000px
                # viewport is a delta far larger than the 640px screen.
                n0 = await b.js("__ptr.length")
                await mouse(b, 'mouseMoved', 1, 1)
                await mouse(b, 'mouseMoved', 999, 699)
                await asyncio.sleep(.2)
                last = await b.js("__ptr[__ptr.length-1]")
                assert await b.js("__ptr.length") > n0, 'the move reached the VM'
                assert 0 <= last['x'] <= 63 and 0 <= last['y'] <= 47, last
                assert last['x'] >= 60 and last['y'] >= 44, ('clamped to the far edge, not dropped', last)
                await mouse(b, 'mouseMoved', 1, 1)
                await asyncio.sleep(.2)
                last = await b.js("__ptr[__ptr.length-1]")
                assert last['x'] == 0 and last['y'] == 0, ('clamped to the near edge', last)
                # ---- 3. a click while grabbed still reaches the VM
                await mouse(b, 'mousePressed', 1, 1, 'left', 1)
                await asyncio.sleep(.1)
                assert await b.js("__ptr.some(p=>p.mask&1)"), 'a button press reached the VM while grabbed'
                await mouse(b, 'mouseReleased', 1, 1, 'left', 0)
                # ---- 4. released: nothing is rewritten any more
                await b.js("document.exitPointerLock()")
                await asyncio.sleep(.2)
                assert await b.js("!document.getElementById('screen').classList.contains('vmc-grabbed')")
                n1 = await b.js("__ptr.length")
                await mouse(b, 'mouseMoved', r[0] + 5, r[1] + 5)
                await asyncio.sleep(.2)
                p = await b.js("__ptr[__ptr.length-1]")
                assert await b.js("__ptr.length") > n1 and p['x'] <= 2 and p['y'] <= 2, ('the ordinary mouse is back', p)
                # ---- 5. the grab can be asked for without clicking the VM (the toolbar button path)
                req = await b.js("__lockReq")
                await b.js("__h.grab()")
                await asyncio.sleep(.1)
                assert await b.js("__lockReq") == req + 1, 'handle.grab() requests the lock'
                errors = await b.js("__errors")
                assert not errors, errors
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
            server.server_close()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome is required')
def test_a_real_click_grabs_and_the_cursor_cannot_leave_the_vm():
    asyncio.run(main())


def test_the_toolbar_offers_the_grab():
    js = (ROOT / 'static/js/client/vms.js').read_text()
    assert 'data-c="grab"' in js, 'the console toolbar has a Grab mouse button'
    assert '.grab()' in js
