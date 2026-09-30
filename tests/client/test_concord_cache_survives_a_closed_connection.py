"""A closed IndexedDB connection must not stop Concord from sending.

Reported from PosterChanOS Communities while sending a message: "Failed to execute 'transaction' on
'IDBDatabase': The database connection is closing." The cache opened ONE connection and reused it
for ever, and only dropped it on a version change. Anything else that closes it -- an app update
restarting underneath the page, cleared site data, the browser reclaiming it -- left every later
transaction throwing that error until a reload. Sending is exactly such a transaction: a message's
encrypted wrap is saved as a pending delivery BEFORE it goes out, and without that store Concord
refuses to send. Real Chrome, the shipped module, the connection closed underneath it.
"""
import asyncio, http.server, json, os, shutil, signal, socket, subprocess, tempfile, threading, unittest, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(ROOT, "static/js/client/concord-cache.js")


class ConcordCacheSurvivesAClosedConnection(unittest.TestCase):
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
        tmp = tempfile.mkdtemp(prefix="pc-concord-closed-")
        server = process = None
        try:
            shutil.copy(MODULE, os.path.join(tmp, "concord-cache.js"))
            page = """<!doctype html><pre id=out></pre><script>
            /* Hold the cache's own connection so the test can close it underneath the module, the way
               an app update, a second window's upgrade or cleared site data does. */
            window.__conns=[];{const o=IDBFactory.prototype.open;IDBFactory.prototype.open=function(){const q=o.apply(this,arguments);q.addEventListener('success',()=>__conns.push(q.result));return q;};}
            </script><script src=concord-cache.js></script><script>
            (async()=>{try{
              const C=PCConcordCache, key='room-A/channel-general', wrap=n=>({id:'wrap-'+n,kind:1059,created_at:n,content:'cipher-'+n,tags:[['p','k']],pubkey:'p',sig:'s'});
              await C.putDelivery(key,wrap(1));
              const before=(await C.getDeliveries(key)).length;
              for(const db of __conns)db.close();             // the connection is now closing/closed
              const r={before};
              try{ await C.putDelivery(key,wrap(2)); r.put='ok'; }catch(e){ r.put=String(e&&e.message||e); }
              try{ r.after=(await C.getDeliveries(key)).map(e=>e.id).sort(); }catch(e){ r.after=String(e&&e.message||e); }
              try{ await C.put(key,[wrap(3)]); r.history=(await C.get(key)).length; }catch(e){ r.history=String(e&&e.message||e); }
              out.textContent=JSON.stringify(r);
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


    def test_sending_saves_its_delivery_after_the_connection_closed(self):
        self.assertNotIn("threw", self.result, self.result)
        self.assertEqual(self.result["before"], 1)
        self.assertEqual(self.result["put"], "ok", "a closed connection still blocks the pending-delivery write that sending needs")
        self.assertEqual(self.result["after"], ["wrap-1", "wrap-2"], self.result)

    def test_history_writes_recover_too(self):
        self.assertEqual(self.result["history"], 1, self.result)


if __name__ == "__main__":
    unittest.main()
