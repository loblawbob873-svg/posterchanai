"""Concord Keep history on this device: the cache obeys the community's policy (docs/CONCORD_RETENTION.md).

  * Forever (the default) is never trimmed -- not by the 5,000-per-channel cap, not by the 32 MB sweep;
  * N days ages out only that stream's rows older than N days;
  * control/guestbook streams are never trimmed (CORD-08 §1) -- the 32 MB sweep used to take them FIRST,
    because they are the oldest rows in the store;
  * a stream with no policy (a community this device has not read yet) keeps the old caps.
"""
import asyncio, http.server, json, os, shutil, signal, socket, subprocess, tempfile, threading, unittest, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(ROOT, "static/js/client/concord-cache.js")


class ConcordKeepHistoryCache(unittest.TestCase):
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
        tmp = tempfile.mkdtemp(prefix="pc-concord-keep-")
        server = process = None
        try:
            shutil.copy(MODULE, os.path.join(tmp, "concord-cache.js"))
            page = """<!doctype html><pre id=out></pre><script src=concord-cache.js></script><script>
            (async()=>{try{
              const C=PCConcordCache; indexedDB.deleteDatabase(C.DB); await new Promise(r=>setTimeout(r,80)); C._reset(); localStorage.clear(); C._resetPolicies();
              const now=Math.floor(Date.now()/1000), ev=(p,i,at,size)=>({id:p+'-'+i,kind:1059,created_at:at,content:size?'x'.repeat(size):'opaque'});
              const many=p=>Array.from({length:5100},(_,i)=>ev(p,i,now-6000+i));
              await C.put('free',many('free'));                                   // no policy: the old 5,000 cap
              await C.put('forever',many('forever'),{keepDays:0});                // Forever: nothing trimmed
              await C.put('week',[ev('week',0,now-30*86400),ev('week',1,now-86400)],{keepDays:7});
              const control=Array.from({length:40},(_,i)=>ev('control',i,1000+i));
              await C.put('control',control,{plane:'control'});                   // the OLDEST rows in the store
              const before={free:(await C.get('free')).length,forever:(await C.get('forever')).length,week:(await C.get('week')).map(e=>e.id)};
              C._resetPolicies();                                                  // let the next write run the byte sweep
              const big=Array.from({length:620},(_,i)=>ev('big',i,now-500+i,60000)); // ~37 MB of no-policy rows
              await C.put('big',big,{limit:10000});
              out.textContent=JSON.stringify({...before,foreverAfter:(await C.get('forever')).length,
                control:(await C.get('control')).length,big:(await C.get('big')).length,
                policies:JSON.parse(localStorage.getItem('pc.concord.keep.v1')||'{}')});
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


    def test_the_cache_follows_each_streams_keep_history_policy(self):
        r = self.result
        self.assertNotIn("threw", r, r)
        self.assertEqual(r["free"], 5000, "a stream with no policy lost its 5,000 cap")
        self.assertEqual(r["forever"], 5100, "a Forever community was trimmed by the per-channel cap")
        self.assertEqual(r["week"], ["week-1"], "7 days kept a 30-day-old message or dropped a recent one")
        self.assertEqual(r["control"], 40, "the 32 MB sweep evicted the control plane")
        self.assertEqual(r["foreverAfter"], 5100, "the 32 MB sweep evicted a Forever community")
        self.assertLess(r["big"], 620, "the byte sweep no longer bounds streams without a policy")
        self.assertEqual(r["policies"], {"forever": 0, "week": 7, "control": "control"}, r["policies"])
