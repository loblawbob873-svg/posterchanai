"""Real-browser coverage for Concord's durable, encrypted relay-envelope cache."""
import asyncio, http.server, json, os, shutil, signal, socket, subprocess, tempfile, threading, unittest, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(ROOT, "static/js/client/concord-cache.js")


class ConcordEnvelopeCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import websockets  # noqa
        except ImportError:
            raise unittest.SkipTest("websockets unavailable")
        chrome = shutil.which("google-chrome-stable") or shutil.which("chromium")
        if not chrome:
            raise unittest.SkipTest("Chrome unavailable")
        cls.result = asyncio.run(cls._run(chrome))

    @classmethod
    async def _run(cls, chrome):
        import websockets
        tmp = tempfile.mkdtemp(prefix="pc-concord-cache-")
        server = process = None
        try:
            shutil.copy(MODULE, os.path.join(tmp, "concord-cache.js"))
            page = """<!doctype html><pre id=out></pre><script src=concord-cache.js></script><script>
            (async()=>{try{
              const C=PCConcordCache, stream='room-A/channel-general';
              if(sessionStorage.getItem('phase')==='reload'){
                const all=await C.get(stream),oldIcon=await C.getIcon('room-A','ref-1'),icon=await C.getIcon('room-A','ref-2'),phase=JSON.parse(sessionStorage.getItem('phase-result'));globalThis.fetch=async()=>{throw new Error('network must not be used')};const iconUrl=URL.createObjectURL(new Blob([icon.bytes],{type:icon.mime}));out.textContent=JSON.stringify({...phase,count:all.length,first:all[0].id,last:all.at(-1).id,plaintext:JSON.stringify(all).includes('decrypted-secret'),oldIcon:!!oldIcon,icon:[...icon.bytes],mime:icon.mime,iconUrl:iconUrl.startsWith('blob:')});return;
              }
              document.body.dataset.step='delete';indexedDB.deleteDatabase(C.DB); await new Promise(r=>setTimeout(r,80)); C._reset();
              const events=Array.from({length:1257},(_,i)=>({id:'id-'+String(i).padStart(4,'0'),kind:1059,created_at:i,content:'cipher-'+i,tags:[['p','wrapped-key-'+i]]}));
              events[17].plaintext='decrypted-secret';events[17].decoded={text:'decrypted-secret'};
              document.body.dataset.step='put';await C.put(stream,events,{limit:2000});
              await C.put(stream,[events[700],events[701]],{limit:2000});
              const all=await C.get(stream),pages=[];let before='';do{const p=await C.page(stream,{before,limit:137});pages.push(...p.events.map(x=>x.id));before=p.before;if(p.done)break;}while(true);
              await C.put('other-room',[{id:'other',kind:1059,created_at:4,content:'other-cipher'}]);
              await C.putIcon('room-A','ref-1',new Uint8Array([1,2,3]),'image/png');
              await C.putIcon('room-A','ref-2',new Uint8Array([7,8,9,10]),'image/webp');
              const hugeIcon=await C.putIcon('too-big','ref',new Uint8Array(C.MAX_ICON_BYTES+1),'image/png'),badMime=await C.putIcon('html','ref',new Uint8Array([60,1]),'text/html');
              const roomA=JSON.stringify(['a:b','c']),roomB=JSON.stringify(['a','b:c']);await C.put(roomA,[{id:'collision-a',kind:1059,content:'cipher'}]);await C.put(roomB,[{id:'collision-b',kind:1059,content:'cipher'}]);const collision=[(await C.get(roomA)).length,(await C.get(roomB)).length];
              const prefixA=JSON.stringify(['prefix-A','c']),prefixB=JSON.stringify(['prefix-AB','c']);await C.put(prefixA,[{id:'prefix-a',kind:1059,content:'cipher'}]);await C.put(prefixB,[{id:'prefix-b',kind:1059,content:'cipher'}]);await C.dropRoom('prefix-A');const prefixDrop=[(await C.get(prefixA)).length,(await C.get(prefixB)).length];
              const invalid=await C.put('invalid',[{id:'wrong-kind',kind:1,content:'plaintext'},{id:'huge',kind:1059,content:'x'.repeat(C.MAX_EVENT_BYTES+1)}]);
              const many=Array.from({length:5100},(_,i)=>({id:'evict-'+i,kind:1059,created_at:i,content:'opaque'}));await C.put('bounded',many);const bounded=await C.get('bounded');
              window.__phase1={count:all.length,first:all[0].id,last:all.at(-1).id,unique:new Set(all.map(x=>x.id)).size,pageCount:pages.length,pageUnique:new Set(pages).size,other:(await C.get('other-room')).length,plaintext:JSON.stringify(all).includes('decrypted-secret')};
              window.__phase1.boundedCount=bounded.length;window.__phase1.boundedFirst=bounded[0].id;window.__phase1.invalid=invalid;window.__phase1.collision=collision;window.__phase1.prefixDrop=prefixDrop;window.__phase1.hugeIcon=hugeIcon;window.__phase1.badMime=badMime;sessionStorage.setItem('phase-result',JSON.stringify(window.__phase1));
              sessionStorage.setItem('phase','reload'); location.reload();
            }catch(e){out.textContent=JSON.stringify({threw:String(e.stack||e)})}})();
            </script>"""
            with open(os.path.join(tmp, 'index.html'), 'w') as f: f.write(page)
            class H(http.server.SimpleHTTPRequestHandler):
                def translate_path(self, path):
                    if path.startswith('/concord-cache.js'): return os.path.join(tmp, 'concord-cache.js')
                    return os.path.join(tmp, 'index.html')
                def log_message(self, *args): pass
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start()
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            url=f'http://127.0.0.1:{server.server_address[1]}/'
            process=subprocess.Popen([chrome,'--headless=new','--disable-gpu','--no-sandbox',f'--remote-debugging-port={port}','--user-data-dir='+os.path.join(tmp,'profile'),url],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            tab=None
            for _ in range(60):
                try:
                    tabs=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list'));tab=next((t for t in tabs if t.get('type')=='page' and t.get('url','').startswith(url)),None)
                    if tab:break
                except Exception: await asyncio.sleep(.25)
            async with websockets.connect(tab['webSocketDebuggerUrl']) as ws:
                seq=0
                async def call(method,params=None):
                    nonlocal seq;seq+=1;await ws.send(json.dumps({'id':seq,'method':method,'params':params or {}}))
                    while True:
                        msg=json.loads(await ws.recv())
                        if msg.get('id')==seq:return msg.get('result',{})
                await call('Runtime.enable');await call('Page.enable')
                for _ in range(120):
                    await asyncio.sleep(.25);r=await call('Runtime.evaluate',{'expression':'document.getElementById("out").textContent','returnByValue':True});value=(r.get('result') or {}).get('value') or ''
                    if value:return json.loads(value)
                debug=await call('Runtime.evaluate',{'expression':'JSON.stringify({href:location.href,step:document.body&&document.body.dataset.step,out:(document.getElementById("out")||{}).textContent})','returnByValue':True})
            raise AssertionError('cache page did not finish: '+str(debug))
        finally:
            if process:
                try:os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=5)
                except Exception:pass
            if server: server.shutdown()
            shutil.rmtree(tmp,ignore_errors=True)

    def test_reload_recovers_every_record_beyond_a_cursor_batch(self):
        self.assertNotIn('threw',self.result);self.assertEqual(self.result['count'],1257);self.assertEqual(self.result['first'],'id-0000');self.assertEqual(self.result['last'],'id-1256')
        self.assertEqual(self.result['unique'],1257);self.assertEqual(self.result['pageCount'],1257);self.assertEqual(self.result['pageUnique'],1257)

    def test_cache_contains_no_decrypted_test_plaintext(self):
        self.assertFalse(self.result['plaintext'])

    def test_malformed_and_oversize_untrusted_records_are_rejected(self):
        self.assertEqual(self.result['invalid'], 0)

    def test_room_keys_do_not_collide_or_prefix_delete_each_other(self):
        self.assertEqual(self.result['collision'], [1, 1])
        self.assertEqual(self.result['prefixDrop'], [0, 1])

    def test_icon_rotation_and_offline_reload_return_exact_bytes(self):
        self.assertFalse(self.result['oldIcon'])
        self.assertEqual(self.result['icon'], [7, 8, 9, 10])
        self.assertEqual(self.result['mime'], 'image/webp')
        self.assertTrue(self.result['iconUrl'])
        self.assertFalse(self.result['hugeIcon'])
        self.assertFalse(self.result['badMime'])

    def test_stream_eviction_is_bounded_and_deterministic(self):
        self.assertEqual(self.result['boundedCount'], 5000)
        self.assertEqual(self.result['boundedFirst'], 'evict-100')


if __name__ == '__main__': unittest.main()
