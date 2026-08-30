#!/usr/bin/env python3
"""Run the real Blossom attachment picker at an Android-sized viewport."""
import asyncio, json, os, re, shutil, subprocess, tempfile, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static/js/client/app.js")
# The runner hands every check its own port (checkall.py: PORT_BASE + index) because the
# browser checks run CONCURRENTLY. Hardcoded, two of them running at once bind the same HTTP
# server port and attach to the same Chrome — the bug that made four checks share 9473. The
# literal stays as the standalone-run default.
# The runner hands every check ONE port (checkall.py: PORT_BASE + index) and it is the BROWSER's.
# Deriving the CDP endpoint as PORT+1 lands on the NEXT job's allocation — a collision that appears
# only under `./test.sh`, never standalone. The static server binds port 0 instead: the OS picks a
# free one, and nothing has to be reserved for it.
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9497)
PORT = 0            # filled in from the listening socket once the server is bound
VIEWPORTS = ((360, 780), (412, 915))


def lift(src, start, end):
    a = src.index(start)
    return src[a:src.index(end, a)]


def page():
    src = open(APP, encoding="utf-8").read()
    picker = lift(src, "  function blossomPicker(", "\n\n  // ---------- Pics:")
    fmt = re.search(r"\n  function _fmtBytes\(.*?\n  \}", src, re.S).group(0)
    return TEMPLATE.replace("/* FUNCTIONS */", fmt + "\n" + picker)


TEMPLATE = r'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css"><body><div id="modal-root"></div>
<script>
window.__errors=[]; addEventListener('error',e=>__errors.push(e.message));
const $=(s,r)=>(r||document).querySelector(s), enc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const FOLDERS=['Camera','Screenshots from an exceptionally long journey','Receipts','Family','Wallpapers','Downloads',...Array.from({length:14},(_,i)=>'Archive '+(i+1))];
const ME={pubkey:'phone-user'}, _MIME_EXT={'image/jpeg':'jpg','application/pdf':'pdf'}, FilesIdx={_lastIndexSha:'index',loadLocal(){},async ensure(){},meta(s){return this.rows[s]||{}},folderOf(s){return this.meta(s).folder||''},folders(){return FOLDERS},isEncFolder(){return false},rows:{}};
const rows=[]; for(let i=0;i<24;i++){const sha=String(i).padStart(64,'0'),pdf=i===0,name=pdf?'Annual-report.pdf':`IMG_${String(i).padStart(4,'0')}_summer-holiday-photo.jpg`,mime=pdf?'application/pdf; charset=binary':'image/jpeg'; rows.push({sha256:sha,url:'https://media.example/'+sha,type:mime,size:102400+i}); FilesIdx.rows[sha]={name,folder:FilesIdx.folders()[i%6],mime};}
const fetch=async()=>({ok:true,async json(){return rows}}), mediaServer=()=> 'https://media.example', toast=()=>{}, _trapFocus=(el,close)=>document.addEventListener('keydown',e=>{if(e.key==='Escape')close()}), _popKeys=()=>{}, _bindThumbFallback=()=>{}, mimeForName=n=>n.endsWith('.pdf')?'application/pdf':'image/jpeg', extOfBlob=(b,m)=>m.name.endsWith('.pdf')?'pdf':'jpg', downloadName=b=>b.sha256, fileLabel=n=>n, blobThumb=(b,e)=>e==='pdf'?'<span class="file-icon">PDF</span>':`<img src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='96' height='96'><rect width='96' height='96' fill='pink'/></svg>" alt="${e}">`;
/* FUNCTIONS */
blossomPicker({value:'',dispatchEvent(){}},x=>window.__picked=x,{title:'Attach Files'});
setTimeout(()=>window.__ready=true,50);
</script>'''


AUDIT = r'''(()=>{
const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)],r=e=>e.getBoundingClientRect(),vis=e=>e&&r(e).width>0&&r(e).height>0;
const grid=q('#bp-grid'),side=q('#bp-folders'),picker=q('.bp-file-picker'),head=q('.bp-head'),explorer=q('.bp-explorer'),trigger=q('.bp-locations'),cards=qa('.bp-pick-card').filter(vis),previews=cards.map(x=>x.querySelector('.bp-pick-preview')),pr=r(picker),hr=r(head);
const smallWidth=r(cards[0]).width, smallCols=new Set(cards.filter(x=>Math.abs(r(x).y-r(cards[0]).y)<2).map(x=>Math.round(r(x).x))).size;
trigger.click(); const folders=qa('.folder-chip').filter(vis);
const out={errors:window.__errors||[],overflow:document.documentElement.scrollWidth>innerWidth+1,fullViewport:Math.abs(pr.x)<1&&Math.abs(pr.y)<1&&Math.abs(pr.width-innerWidth)<1&&Math.abs(pr.height-innerHeight)<1,compactHeader:hr.height>=48&&hr.height<=72,contentHeight:r(explorer).height,drawerOpened:explorer.classList.contains('bp-locations-on')&&trigger.getAttribute('aria-expanded')==='true',folders:folders.length,cards:cards.length,smallCols,tile:{w:Math.round(r(cards[0]).width*10)/10,h:Math.round(r(cards[0]).height*10)/10},preview:{w:Math.round(r(previews[0]).width*10)/10,h:Math.round(r(previews[0]).height*10)/10},vertical:folders.length>1&&Math.abs(r(folders[0]).x-r(folders[1]).x)<2&&r(folders[1]).y>r(folders[0]).y,folderReadable:folders.every(x=>r(x).width>=220&&r(x).height>=36&&x.scrollWidth<=x.clientWidth+2),sideScroll:side.scrollHeight>side.clientHeight,gridScroll:grid.scrollHeight>grid.clientHeight,square:previews.every(x=>Math.abs(r(x).width-r(x).height)<2),nonImageSquare:!!cards[0].querySelector('.file-icon')&&Math.abs(r(cards[0].querySelector('.file-icon')).width-r(cards[0].querySelector('.file-icon')).height)<2,fileReadable:cards.every(x=>r(x).width>=44&&r(x).height>=44&&x.querySelector('.fname')&&x.querySelector('small')&&(x.querySelector('img')||x.querySelector('.file-icon')))};
document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}));out.escapeClosed=!explorer.classList.contains('bp-locations-on');trigger.click();grid.click();out.backdropClosed=!explorer.classList.contains('bp-locations-on');trigger.click();folders[1].click();out.folderFiltered=qa('.bp-pick-card').filter(vis).length===4;trigger.click();folders[0].click();out.allRestored=qa('.bp-pick-card').filter(vis).length===24;out.drawerClosed=!explorer.classList.contains('bp-locations-on')&&trigger.getAttribute('aria-expanded')==='false';out.fullWidthAfterSelect=r(grid).width>=innerWidth-4;out.usableContent=out.contentHeight>=innerHeight*.80;
q('[data-bp-size="medium"]').click();out.toggleEffect=explorer.classList.contains('bp-medium')&&r(qa('.bp-pick-card')[0]).width>smallWidth+20&&q('[data-bp-size="medium"]').getAttribute('aria-pressed')==='true';qa('.bp-pick-card')[0].click();out.picked=!!window.__picked;out.pickedMetadata=!!window.__picked&&window.__picked.name==='Annual-report.pdf'&&window.__picked.type==='application/pdf; charset=binary'&&window.__picked.ext==='pdf'&&/^https:\/\/media\.example\//.test(window.__picked.url);return out;})()'''


async def run():
    try: import websockets
    except ImportError: print('SKIP no websockets module'); return 2
    chrome = next((shutil.which(x) for x in ('chromium','google-chrome','google-chrome-stable') if shutil.which(x)), None)
    if not chrome: print('SKIP no Chrome'); return 2
    td=tempfile.TemporaryDirectory(); html=page()
    class H(__import__('http.server').server.SimpleHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_GET(self):
            if self.path=='/': self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(html.encode());return
            self.path=self.path.split('?',1)[0]; return super().do_GET()
    import threading, http.server
    os.chdir(ROOT); srv=http.server.ThreadingHTTPServer(('127.0.0.1',0),H);PORT=srv.server_address[1]; threading.Thread(target=srv.serve_forever,daemon=True).start()
    proc=subprocess.Popen([chrome,'--headless=new','--no-sandbox',f'--remote-debugging-port={CDP_PORT}',f'--user-data-dir={td.name}','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        tab=None
        for _ in range(40):
            try:
                tabs=json.load(urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/list'))
                tab=next(t for t in tabs if t.get('type')=='page' and not t.get('url','').startswith('chrome-extension:'));break
            except Exception: await asyncio.sleep(.2)
        async with websockets.connect(tab['webSocketDebuggerUrl']) as ws:
            n=0
            async def call(method,params={}):
                nonlocal n;n+=1;await ws.send(json.dumps({'id':n,'method':method,'params':params}))
                while True:
                    m=json.loads(await ws.recv())
                    if m.get('id')==n:return m.get('result',{})
            await call('Page.enable')
            await call('Runtime.enable')
            results=[]
            for width,height in VIEWPORTS:
                await call('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':3,'mobile':True})
                await call('Page.navigate',{'url':f'http://127.0.0.1:{PORT}/'})
                await asyncio.sleep(.3)
                for _ in range(40):
                    await asyncio.sleep(.1); z=await call('Runtime.evaluate',{'expression':'window.__ready===true','returnByValue':True})
                    if z.get('result',{}).get('value'):break
                z=await call('Runtime.evaluate',{'expression':AUDIT,'returnByValue':True}); out=z.get('result',{}).get('value')
                if not out and z.get('exceptionDetails'):
                    why=await call('Runtime.evaluate',{'expression':'JSON.stringify({url:location.href,errors:window.__errors,body:document.body.innerHTML.slice(0,500)})','returnByValue':True})
                    print('audit exception',z['exceptionDetails'],why)
                required={'errors':[], 'overflow':False,'fullViewport':True,'compactHeader':True,'usableContent':True,'drawerOpened':True,'escapeClosed':True,'backdropClosed':True,'folderFiltered':True,'allRestored':True,'drawerClosed':True,'fullWidthAfterSelect':True,'folders':21,'cards':24,'smallCols':3 if width==360 else 4,'vertical':True,'folderReadable':True,'sideScroll':True,'gridScroll':True,'square':True,'nonImageSquare':True,'fileReadable':True,'toggleEffect':True,'picked':True,'pickedMetadata':True}
                bad={k:(out and out.get(k),v) for k,v in required.items() if not out or out.get(k)!=v}
                if bad: print(f'Blossom picker FAIL at {width}x{height}',bad,'actual=',out);return 1
                results.append({'viewport':f'{width}x{height}', **out})
            print('Blossom picker mobile runtime/layout: clean',results);return 0
    finally: proc.terminate();srv.shutdown();td.cleanup()

if __name__=='__main__': raise SystemExit(asyncio.run(run()))
