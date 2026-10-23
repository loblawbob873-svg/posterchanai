"""THE INSTALLED PWA, AS A PHONE HOLDS IT: one Chrome PROFILE that outlives its browser, the SHIPPED
service worker (static/js/client/sw.js), and a node that can be redeployed or switched off under it.

The other full-app harnesses start a fresh profile per test and never let the service worker matter:
the page is served straight from the test server, and "offline" is a WebSocket stub. Every flow that
only exists BECAUSE of the worker is invisible there:

  * an UPDATE -- build N is installed, build N+1 is deployed, and the worker decides what the next
    load (or the open tab) runs;
  * a COLD START OFFLINE -- the browser is closed, the network is gone, and the app is launched again
    from nothing but what the worker kept;
  * a WEB PUSH reaching a CLOSED app -- the worker is woken with the payload and draws the card.

Here those are real: `Node` serves two builds of the client (the shipped files, plus per-build
overrides) on ONE origin that can be stopped and started again on the same port -- the origin is
what the worker, its caches and localStorage are keyed on -- and `Phone` is one Chrome user-data-dir
that can be closed and relaunched (`cold_start`). Nothing leaves the machine: https/wss are blocked
and every non-loopback WebSocket is pointed at this node's relay.

Fail-first: PC_PWA_OVERRIDE="/static/js/client/sw.js=/tmp/broken-sw.js,..." serves another copy of a
shipped file in EVERY build (the PC_JOURNEY_OVERRIDE mechanism of the other harnesses).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets
from jinja2 import Template

ROOT = Path(__file__).resolve().parents[2]
CHROME = '/opt/google/chrome/chrome'
SW_PATH = 'static/js/client/sw.js'


def chrome_ok() -> bool:
    return Path(CHROME).exists()


def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def shipped_cache_version() -> int:
    m = re.search(r"const CACHE = 'pc-nostr-v(\d+)'", (ROOT / SW_PATH).read_text())
    assert m, 'CACHE not found in sw.js'
    return int(m.group(1))


def _env_overrides() -> dict:
    out = {}
    for pair in filter(None, os.environ.get('PC_PWA_OVERRIDE', '').split(',')):
        k, v = pair.split('=', 1)
        out[k.strip()] = Path(v.strip()).read_bytes()
    return out


MARK = "\n;(window.__PC_MARKS=window.__PC_MARKS||{})[%s]=%s;\n"


class Build:
    """One deployable build of the client: the shipped tree plus overrides.

    `mark` stamps each named module with `window.__PC_MARKS[<file>] = <tag>` (one appended line), so a
    page can say, module by module, which build it is actually running."""

    def __init__(self, tag: str, ver: str, *, cache: int | None = None, mark=(), overrides=None):
        self.tag, self.ver = tag, ver
        self.files: dict[str, bytes] = dict(_env_overrides())
        self.files.update(overrides or {})
        cache = shipped_cache_version() if cache is None else cache
        sw = self.files.get('/' + SW_PATH) or (ROOT / SW_PATH).read_bytes()
        self.files['/' + SW_PATH] = re.sub(rb"const CACHE = 'pc-nostr-v\d+'",
                                           b"const CACHE = 'pc-nostr-v%d'" % cache, sw)
        self.cache = 'pc-nostr-v%d' % cache
        for name in mark:
            path = '/static/js/client/' + name
            body = self.files.get(path) or (ROOT / path.lstrip('/')).read_bytes()
            self.files[path] = body + (MARK % (json.dumps(name), json.dumps(tag))).encode()

    def get(self, path: str) -> bytes | None:
        if path in self.files:
            return self.files[path]
        p = (ROOT / path.lstrip('/')).resolve()
        if not str(p).startswith(str(ROOT / 'static')) or not p.is_file():
            return None
        return p.read_bytes()

    def shell(self) -> bytes:
        return Template((ROOT / 'templates/client.html').read_text()).render(
            ver=self.ver, build='b-' + self.tag, nostr_only=False, secure=False, meta=None,
            default_theme='', registration_enabled=False).encode()


def _match(f, ev):
    if f.get('ids') and ev['id'] not in f['ids']:
        return False
    if f.get('authors') and ev['pubkey'] not in f['authors']:
        return False
    if f.get('kinds') and ev['kind'] not in f['kinds']:
        return False
    if f.get('since') and ev['created_at'] < f['since']:
        return False
    if f.get('until') and ev['created_at'] > f['until']:
        return False
    for k, want in f.items():
        if k.startswith('#') and len(k) == 2:
            vals = [t[1] for t in ev.get('tags', []) if t and t[0] == k[1] and len(t) > 1]
            if not any(v in want for v in vals):
                return False
    return True


class Relay:
    """A NIP-01 relay on a FIXED port that can be switched off and on again (the network going away)."""

    def __init__(self, port: int | None = None):
        self.port = port or free_port()
        self.events: dict[str, dict] = {}
        self.received: list[dict] = []          # every EVENT frame, duplicates included
        self._loop = None
        self._thread = None
        self._stop = None
        self._conns = set()

    async def _handler(self, ws):
        self._conns.add(ws)
        subs = {}
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, list) or not m:
                    continue
                if m[0] == 'EVENT' and len(m) > 1 and isinstance(m[1], dict):
                    ev = m[1]
                    self.received.append(ev)
                    self.events.setdefault(ev['id'], ev)
                    await ws.send(json.dumps(['OK', ev['id'], True, '']))
                    for sid, flts in list(subs.items()):
                        if any(_match(f, ev) for f in flts):
                            await ws.send(json.dumps(['EVENT', sid, ev]))
                elif m[0] == 'REQ' and len(m) > 2:
                    sid, flts = m[1], [f for f in m[2:] if isinstance(f, dict)]
                    subs[sid] = flts
                    seen = set()
                    for f in flts:
                        hits = sorted((e for e in self.events.values() if _match(f, e)),
                                      key=lambda e: -e['created_at'])[:int(f.get('limit') or 500)]
                        for e in hits:
                            if e['id'] not in seen:
                                seen.add(e['id'])
                                await ws.send(json.dumps(['EVENT', sid, e]))
                    await ws.send(json.dumps(['EOSE', sid]))
                elif m[0] == 'CLOSE' and len(m) > 1:
                    subs.pop(m[1], None)
                elif m[0] == 'COUNT' and len(m) > 2:
                    n = sum(1 for e in self.events.values() if any(_match(f, e) for f in m[2:]))
                    await ws.send(json.dumps(['COUNT', m[1], {'count': n}]))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._conns.discard(ws)

    def start(self):
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._stop = asyncio.Event()

            async def main():
                async with websockets.serve(self._handler, '127.0.0.1', self.port, max_size=20_000_000,
                                            reuse_address=True):
                    ready.set()
                    await self._stop.wait()
                    for c in list(self._conns):
                        await c.close()
            self._loop.run_until_complete(main())
            self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(10), 'relay did not start'
        return self

    def stop(self):
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
            self._thread.join(10)
        self._loop = self._stop = None

    def add(self, ev):
        self.events[ev['id']] = ev


class Node:
    """The web client's server on a fixed origin: `/client`, `/client/sw.js`, `/static/*`, the config.

    `deploy(build)` swaps what it serves (exactly what sync.sh does to a node). `down()`/`up()` stop and
    restart BOTH the HTTP server and the relay on the same ports -- from the device, the network is gone.
    `routes` are (method, path) -> fn(handler, body) for anything a test needs to answer itself.
    `proxy` maps a path prefix to another local base URL (e.g. the real FastAPI push routes)."""

    def __init__(self, build: Build):
        self.build = build
        self.port = free_port()
        self.relay = Relay().start()
        self.requests: list[tuple[str, str]] = []
        self.routes: dict = {}
        self.proxy: dict[str, str] = {}
        self._srv = None
        self.up()

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.port}'

    def deploy(self, build: Build):
        self.build = build

    def _handler(node):
        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def _send(self, status, body: bytes, ctype, extra=None):
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)

            def _json(self, obj, status=200):
                self._send(status, json.dumps(obj).encode(), 'application/json', {'Cache-Control': 'no-store'})

            def _body(self):
                n = int(self.headers.get('Content-Length') or 0)
                return self.rfile.read(n) if n else b''

            def _proxy(self, base, body):
                import httpx
                r = httpx.request(self.command, base + self.path, content=body or None,
                                  headers={'Content-Type': self.headers.get('Content-Type') or 'application/json'},
                                  timeout=60, trust_env=False)
                self._send(r.status_code, r.content, r.headers.get('content-type', 'application/json'))

            def _any(self):
                path = self.path.split('?')[0]
                body = self._body() if self.command in ('POST', 'PUT') else b''
                node.requests.append((self.command, self.path))
                fn = node.routes.get((self.command, path))
                if fn:
                    return fn(self, body)
                for prefix, base in node.proxy.items():
                    if path.startswith(prefix):
                        return self._proxy(base, body)
                b = node.build
                if path in ('/client', '/client/') and self.command in ('GET', 'HEAD'):
                    return self._send(200, b.shell(), 'text/html; charset=utf-8',
                                      {'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0'})
                if path == '/client/sw.js':
                    return self._send(200, b.get('/' + SW_PATH), 'application/javascript',
                                      {'Service-Worker-Allowed': '/client', 'Cache-Control': 'no-cache'})
                if path.startswith('/client/config'):
                    url = f'ws://127.0.0.1:{node.relay.port}'
                    return self._json({'nostr_only': False, 'relay_url': url, 'relays': [url],
                                       'default_relays': [url], 'blossom_enabled': False})
                if path.startswith('/static/'):
                    data = b.get(path)
                    if data is None:
                        return self._send(404, b'not found', 'text/plain')
                    ctype = ('application/javascript' if path.endswith('.js') else 'text/css' if path.endswith('.css')
                             else 'image/png' if path.endswith('.png') else 'image/webp' if path.endswith('.webp')
                             else 'application/json' if path.endswith('.json') else 'application/octet-stream')
                    # What Cloudflare does to these in production: a 31-day max-age. The ?v= token on
                    # the page's tags is the only thing that moves a browser off an old copy.
                    return self._send(200, data, ctype, {'Cache-Control': 'public, max-age=2678400'})
                if path.startswith('/api/') or path.startswith('/client/'):
                    return self._json({})
                return self._send(404, b'not found', 'text/plain')

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _any

            def do_OPTIONS(self):
                self._send(204, b'', 'text/plain')
        return H

    def up(self):
        if self._srv is not None:
            return
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.daemon_threads = True
        for _ in range(50):
            try:
                self._srv = ThreadingHTTPServer(('127.0.0.1', self.port), self._handler())
                break
            except OSError:
                time.sleep(.1)
        assert self._srv, 'could not rebind the node port'
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        if self.relay._loop is None:
            self.relay.start()

    def down(self):
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        self.relay.stop()

    def close(self):
        self.down()


# ---- Chrome over CDP, with sessions (a page, the service worker) ----------------------------------

# Every document load appends {ver, marks} to sessionStorage.__loads once the page's own scripts ran,
# so a test can see EACH load -- including one the app reloads away from by itself.
LOAD_LOG = r'''
addEventListener('load',()=>{try{const s=JSON.parse(sessionStorage.getItem('__loads')||'[]');
  s.push({ver:String(window.__VER),marks:Object.assign({},window.__PC_MARKS||{}),at:Date.now()});
  sessionStorage.setItem('__loads',JSON.stringify(s));}catch(_){}});
'''
LOOPBACK_ONLY = r'''
(()=>{const W=window.WebSocket;if(!W||W.__pcLoopback)return;
  const L=function(u,p){if(!/^wss?:\/\/(127\.0\.0\.1|localhost)[:/]/i.test(String(u)))u='ws://127.0.0.1:__RELAY__/';
    return p===undefined?new W(u):new W(u,p);};
  L.prototype=W.prototype;L.CONNECTING=0;L.OPEN=1;L.CLOSING=2;L.CLOSED=3;L.__pcLoopback=true;window.WebSocket=L;})();
'''
FIRST_RUN_DONE = r'''
try{const s=JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}');if(!('experience' in s)){s.experience='keep';s.osMode=false;localStorage.setItem('pc_nostr_settings',JSON.stringify(s));}}catch(_){}
window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message||e)));
window.__toasts=[];
document.addEventListener('DOMContentLoaded',()=>{const r=document.getElementById('toast-root');if(r)new MutationObserver(ms=>{
  for(const m of ms)for(const n of m.addedNodes)__toasts.push(String(n.textContent||''));}).observe(r,{childList:true});});
'''


class CDPError(AssertionError):
    pass


class CDP:
    """One browser-level DevTools socket; `session` picks the target (a page, a worker)."""

    def __init__(self, ws):
        self.ws = ws
        self.seq = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.events: list[dict] = []
        self._reader = asyncio.ensure_future(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                if 'id' in m and m['id'] in self.pending:
                    fut = self.pending.pop(m['id'])
                    if not fut.done():
                        fut.set_result(m)
                elif m.get('method'):
                    self.events.append(m)
                    del self.events[:-500]
        except Exception:
            pass
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(CDPError('devtools socket closed'))

    async def call(self, method, params=None, session=None, timeout=60):
        self.seq += 1
        msg = {'id': self.seq, 'method': method, 'params': params or {}}
        if session:
            msg['sessionId'] = session
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.seq] = fut
        await self.ws.send(json.dumps(msg))
        m = await asyncio.wait_for(fut, timeout)
        if 'error' in m:
            raise CDPError(f'{method}: {m["error"]}')
        return m.get('result', {})

    async def close(self):
        self._reader.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass


class Page:
    def __init__(self, cdp: CDP, session: str, target: str):
        self.cdp, self.session, self.target = cdp, session, target

    async def call(self, method, params=None, timeout=60):
        return await self.cdp.call(method, params, self.session, timeout)

    async def js(self, expression, timeout=60):
        r = await self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                                 'awaitPromise': True}, timeout)
        if 'exceptionDetails' in r:
            raise CDPError(f'JS threw: {r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"]}')
        return r['result'].get('value')

    async def until(self, expression, seconds=40, what=None):
        end = time.monotonic() + seconds
        last = None
        while time.monotonic() < end:
            try:
                last = await self.js(expression, timeout=20)
                if last:
                    return last
            except (CDPError, asyncio.TimeoutError) as e:
                last = repr(e)
            await asyncio.sleep(.2)
        state = None
        try:
            state = await self.js("({url:location.href,errors:(window.__errors||[]).slice(-8),"
                                  "toasts:(window.__toasts||[]).slice(-8),"
                                  "body:(document.body&&document.body.innerText||'').slice(0,600),"
                                  "html:document.documentElement.outerHTML.length,ready:document.readyState,"
                                  "worker:!!(navigator.serviceWorker&&navigator.serviceWorker.controller),"
                                  "missing:['NostrTools','Store','Relay','Outbox','PCConcord','__PC'].filter(g=>!(g in window)),"
                                  "hidden:getComputedStyle(document.body).display+'/'+getComputedStyle(document.body).visibility+'/'+document.body.className})", timeout=10)
        except Exception as e:
            state = repr(e)
        mine = [e for e in self.cdp.events if e.get('sessionId') == self.session]
        urls = {e['params']['requestId']: e['params']['request']['url'] for e in mine
                if e['method'] == 'Network.requestWillBeSent'}
        failed = [(urls.get(e['params']['requestId'], '?')[-80:], e['params'].get('errorText'))
                  for e in mine if e['method'] == 'Network.loadingFailed'][:12]
        js = [(e['params']['response']['url'].split('/')[-1][:40], e['params']['response']['status'])
              for e in mine if e['method'] == 'Network.responseReceived'
              and '/static/js/client/app.js' in e['params']['response']['url']]
        failed.append(('app.js responses', js))
        logs = [str(e['params'].get('exceptionDetails', {}).get('exception', {}).get('description')
                    or e['params'].get('exceptionDetails', {}).get('text'))[:200]
                for e in mine if e['method'] == 'Runtime.exceptionThrown'][:5]
        raise AssertionError(f'gave up waiting for {what or expression!r} (last={last!r}); page: {state}; '
                             f'failed loads: {failed}; exceptions: {logs}')


class Phone:
    """One installed PWA: a Chrome profile that outlives the browser process.

    `async with Phone(node) as ph:` launches it; `await ph.open()` opens the app in a new tab;
    `await ph.cold_start()` kills the browser and launches it again on the SAME profile -- the app
    closed by the OS and opened from the home screen."""

    def __init__(self, node: Node, sk_fill=7):
        self.node = node
        self.sk_fill = sk_fill
        self.tmp = tempfile.mkdtemp(prefix='pc-pwa-')
        self.proc = None
        self.cdp: CDP | None = None

    async def _launch(self):
        port_file = Path(self.tmp, 'DevToolsActivePort')
        try:
            port_file.unlink()
        except FileNotFoundError:
            pass
        self.proc = subprocess.Popen([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
                                      '--no-first-run', '--no-default-browser-check',
                                      '--remote-debugging-port=0', '--user-data-dir=' + self.tmp, 'about:blank'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(300):
            try:
                lines = port_file.read_text().splitlines()
                if len(lines) >= 2 and lines[0].isdigit():
                    break
            except FileNotFoundError:
                pass
            await asyncio.sleep(.1)
        lines = port_file.read_text().splitlines()
        url = f'ws://127.0.0.1:{lines[0]}{lines[1]}'
        ws = await websockets.connect(url, max_size=100_000_000)
        self.cdp = CDP(ws)
        await self.cdp.call('Browser.grantPermissions', {'origin': self.node.origin,
                                                         'permissions': ['notifications']})
        await self.cdp.call('Target.setDiscoverTargets', {'discover': True})

    async def __aenter__(self):
        await self._launch()
        return self

    async def __aexit__(self, *exc):
        await self.quit()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def quit(self):
        if self.cdp:
            try:
                await asyncio.wait_for(self.cdp.call('Browser.close'), 5)
            except Exception:
                pass
            await self.cdp.close()
            self.cdp = None
        if self.proc:
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None

    async def cold_start(self):
        await self.quit()
        await self._launch()

    async def open(self, path='/client', init='', wait_boot=True, width=1280, height=900) -> Page:
        t = await self.cdp.call('Target.createTarget', {'url': 'about:blank'})
        s = await self.cdp.call('Target.attachToTarget', {'targetId': t['targetId'], 'flatten': True})
        page = Page(self.cdp, s['sessionId'], t['targetId'])
        await page.call('Page.enable')
        await page.call('Runtime.enable')
        await page.call('Network.enable')
        await page.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
        await page.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height,
                                                               'deviceScaleFactor': 1, 'mobile': width < 600})
        await page.call('Page.addScriptToEvaluateOnNewDocument',
                        {'source': FIRST_RUN_DONE + LOAD_LOG + LOOPBACK_ONLY.replace('__RELAY__', str(self.node.relay.port)) + init})
        await page.call('Page.navigate', {'url': self.node.origin + path})
        if wait_boot:
            await page.until('!!window.__PC && document.readyState==="complete"', 60, 'the app to boot')
        return page

    async def close_page(self, page: Page):
        await self.cdp.call('Target.closeTarget', {'targetId': page.target})

    async def login(self, page: Page):
        await page.until("document.body.classList.contains('guest') || !!(__PC.me&&__PC.me())", 40, 'login screen')
        if not await page.js('!!(__PC.me&&__PC.me())'):
            await page.js("(()=>{const key=new Uint8Array(32).fill(%d);document.querySelector('#nsec-input').value="
                          "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()"
                          % self.sk_fill)
        await page.until('!!(__PC.me&&__PC.me())', 40, 'signed in')

    async def sw_ready(self, page: Page, cache: str):
        """The worker of `cache` controls this page and has finished precaching."""
        await page.until("!!navigator.serviceWorker.controller", 60, 'a controlling service worker')
        await page.until("caches.has(%s)" % json.dumps(cache), 60, f'cache {cache}')
        # install's waitUntil resolves only after every SHELL entry settled; activate after that.
        await page.until("navigator.serviceWorker.getRegistration('/client').then(r=>!!(r&&r.active&&r.active.state==='activated'&&!r.installing&&!r.waiting))",
                         60, 'the worker to be activated')

    async def worker(self):
        """A session attached to the /client service worker (started on demand)."""
        for _ in range(100):
            ts = (await self.cdp.call('Target.getTargets'))['targetInfos']
            sw = [t for t in ts if t['type'] == 'service_worker' and t['url'].startswith(self.node.origin + '/client/sw.js')]
            if sw:
                s = await self.cdp.call('Target.attachToTarget', {'targetId': sw[0]['targetId'], 'flatten': True})
                p = Page(self.cdp, s['sessionId'], sw[0]['targetId'])
                await p.call('Runtime.enable')
                return p
            await asyncio.sleep(.1)
        raise AssertionError('no service worker target')
