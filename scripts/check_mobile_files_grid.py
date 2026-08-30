#!/usr/bin/env python3
"""Runtime geometry/interaction check for the phone Files icon grid."""
import asyncio, json, os, shutil, subprocess, tempfile, threading, urllib.request
import http.server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWPORTS = ((360, 780), (412, 915))
# The runner hands every check its own port (checkall.py: PORT_BASE + index) because the
# browser checks run CONCURRENTLY. Hardcoded, two of them running at once bind the same HTTP
# server port and attach to the same Chrome — the bug that made four checks share 9473. The
# literal stays as the standalone-run default.
# The runner hands every check ONE port (checkall.py: PORT_BASE + index) and it is the BROWSER's.
# Deriving the CDP endpoint as PORT+1 lands on the NEXT job's allocation — a collision that appears
# only under `./test.sh`, never standalone. The static server binds port 0 instead: the OS picks a
# free one, and nothing has to be reserved for it.
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9503)
PORT = 0            # filled in from the listening socket once the server is bound

PAGE = r'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css"><body>
<main class="feed feed-files"><div id="files-pane"><div class="fx-explorer"><section class="fx-main">
<div class="files-grid" id="bl-grid"></div></section></div></div></main>
<script>
const grid=document.querySelector('#bl-grid');
const image=`<img alt="photo" src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='90'><rect width='160' height='90' fill='%2356b'/></svg>">`;
const icon='<div class="file-icon">📄<span>pdf</span></div>';
grid.innerHTML=Array.from({length:18},(_,i)=>`<div class="file-card" data-sha="${i}"><a href="#file-${i}">${i%2?image:icon}</a><input type="checkbox" class="selbox"><div class="meta"><span class="fname">File ${i}</span></div></div>`).join('');
grid.querySelectorAll('.file-card>a').forEach(a=>a.onclick=e=>{e.preventDefault();window.__opened=a.closest('.file-card').dataset.sha});
grid.querySelectorAll('.selbox').forEach(c=>c.onclick=e=>{e.stopPropagation();c.closest('.file-card').classList.toggle('selected',c.checked)});
window.__ready=true;
</script>'''

AUDIT = r'''(()=>{const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)],b=e=>e.getBoundingClientRect();
const cards=qa('.file-card'), previews=cards.map(c=>c.querySelector('a>*')), first=b(cards[0]);
const cols=new Set(cards.filter(c=>Math.abs(b(c).y-first.y)<2).map(c=>Math.round(b(c).x))).size;
const boxes=previews.map(p=>({w:Math.round(b(p).width*10)/10,h:Math.round(b(p).height*10)/10}));
cards[1].querySelector('a').click(); const cb=cards[0].querySelector('.selbox');cb.click();
return {viewport:[innerWidth,innerHeight],cols,tile:{w:Math.round(first.width*10)/10,h:Math.round(first.height*10)/10},preview:boxes[0],imagePreview:boxes[1],allSquare:boxes.every(x=>Math.abs(x.w-x.h)<1),overflow:document.documentElement.scrollWidth>innerWidth+1,opened:window.__opened==='1',selected:cards[0].classList.contains('selected')&&cb.checked,gridScroll:q('#bl-grid').scrollHeight>innerHeight};})()'''

async def run():
    try: import websockets
    except ImportError: print('SKIP no websockets module'); return 2
    chrome = next((shutil.which(x) for x in ('google-chrome-stable','chromium','google-chrome') if shutil.which(x)), None)
    if not chrome: print('SKIP no Chrome'); return 2
    td=tempfile.TemporaryDirectory()
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_GET(self):
            if self.path=='/': self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(PAGE.encode());return
            self.path=self.path.split('?',1)[0];return super().do_GET()
    old=os.getcwd();os.chdir(ROOT);srv=http.server.ThreadingHTTPServer(('127.0.0.1',0),H);PORT=srv.server_address[1];threading.Thread(target=srv.serve_forever,daemon=True).start()
    proc=subprocess.Popen([chrome,'--headless=new','--no-sandbox',f'--remote-debugging-port={CDP_PORT}',f'--user-data-dir={td.name}','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        tab=None
        for _ in range(40):
            try:
                tabs=json.load(urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/list'));tab=next(t for t in tabs if t.get('type')=='page');break
            except Exception: await asyncio.sleep(.2)
        if not tab: print('SKIP could not start Chrome');return 2
        results=[]
        async with websockets.connect(tab['webSocketDebuggerUrl']) as ws:
            n=0
            async def call(method,params=None):
                nonlocal n;n+=1;await ws.send(json.dumps({'id':n,'method':method,'params':params or {}}))
                while True:
                    msg=json.loads(await ws.recv())
                    if msg.get('id')==n:return msg.get('result',{})
            await call('Page.enable');await call('Runtime.enable')
            for width,height in VIEWPORTS:
                await call('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':3,'mobile':True})
                await call('Page.navigate',{'url':f'http://127.0.0.1:{PORT}/'})
                for _ in range(40):
                    await asyncio.sleep(.1);z=await call('Runtime.evaluate',{'expression':'window.__ready===true','returnByValue':True})
                    if z.get('result',{}).get('value'):break
                z=await call('Runtime.evaluate',{'expression':AUDIT,'returnByValue':True});out=z.get('result',{}).get('value')
                expected={'cols':3 if width==360 else 4,'allSquare':True,'overflow':False,'opened':True,'selected':True}
                bad={k:(out and out.get(k),v) for k,v in expected.items() if not out or out.get(k)!=v}
                if bad: print(f'Mobile Files grid FAIL at {width}x{height}',bad,'actual=',out);return 1
                results.append(out)
        print('Mobile Files grid runtime/layout: clean',results);return 0
    finally:
        proc.terminate();srv.shutdown();td.cleanup();os.chdir(old)

if __name__=='__main__': raise SystemExit(asyncio.run(run()))
