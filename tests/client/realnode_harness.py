"""A LOCAL NODE WITH A REAL RELAY, for driving the shipped web client the way a member's phone does.

The other full-app harnesses stub the relay (an in-page FixtureSocket, or wallpaper_harness's small NIP-01
server). The reports of 2026-10-10..13 were all about what a REAL node does to a REAL client under REAL load:
a tunnel cap full of live subscriptions, a relay that refuses a write, an AUTH that has to be answered. So:

  Home      THIS node's relay: the server role's own RelayServer over its own PosterChanDB store
            (app/services/nostr_relay/server.py + pcdb_store.py -- NIP-42 AUTH, the WoT gate, NIP-78 reads,
            exactly as on a node), behind a front that also serves `/ws/concord-relay` -- Concord's tunnel --
            enforcing the SHIPPED app/services/concord_tunnel.py share (`acquire`/`release`, PER_MEMBER).
  FarRelay  another relay on the internet (a Concord community's own relay): its own store, NIP-01 only.
  Instance  the web client's server: the shell and static files (wallpaper_harness.Instance), its config
            naming Home, the tunnel ticket, the drive index and Blossom.
  Foreign   a Blossom server that is NOT PosterChan's (BUD-01: GET /<sha>, GET /list/<pk>, nothing else).
  Device    one Chrome profile at a given size (390x844 phone or 1440x900 desktop), signed in WITH AN NSEC
            through the login screen, as a person does.

Nothing leaves the machine: every non-loopback socket is pointed at an empty local relay
(wallpaper_harness.LOOPBACK_ONLY) and every https:// request is blocked.

`PC_CONCORD_TUNNEL_SRC` (a path to an older concord_tunnel.py) runs the tunnel share of that version -- the
fail-first runs; `OVERRIDES` serves an older copy of one shipped client file.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.client import wallpaper_harness as H  # noqa: E402
from tests.client import test_effects_full_app as full  # noqa: E402

CHROME = H.CHROME
TICKET = 'f' * 64 + '.9999999999.' + 'a' * 40


def chrome_ok() -> bool:
    return Path(CHROME).exists()


def tunnel_module():
    """The tunnel share to enforce: the shipped module, or an older copy named by PC_CONCORD_TUNNEL_SRC."""
    src = os.environ.get('PC_CONCORD_TUNNEL_SRC')
    if not src:
        from app.services import concord_tunnel
        return concord_tunnel
    spec = importlib.util.spec_from_file_location('pc_tunnel_under_test', src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sign(sk_fill: int, kind: int, content: str = '', tags=None, created_at=None) -> dict:
    from app.services.nostr.event import build_event
    return build_event(bytes([sk_fill]) * 32, kind, content, tags or [], created_at)


def pubkey(sk_fill: int) -> str:
    return sign(sk_fill, 1, 'x')['pubkey']


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
            if not any(t and t[0] == k[1] and len(t) > 1 and t[1] in want for t in ev.get('tags', [])):
                return False
    return True


class _Loop:
    """One asyncio loop on its own thread; everything a server here does runs on it."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def serve(self, handler, process_request):
        async def go():
            return await websockets.serve(handler, '127.0.0.1', 0, max_size=None, process_request=process_request)
        return self.run(go())


class FarRelay:
    """A relay elsewhere: its own events, NIP-01, records what it was sent. `down=True` refuses every socket."""

    def __init__(self, loop: _Loop, name=''):
        self.name, self.events, self.writes, self.subs = name, [], [], {}
        self.reqs = []         # every REQ's filters, in order
        self.down = False
        self._loop = loop
        self.server = loop.serve(self._handler, self._pr)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = 'ws://127.0.0.1:%d' % self.port

    def _pr(self, conn, req):
        if self.down:
            return conn.respond(503, 'down')
        return None

    async def _handler(self, ws):
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, list) or not m:
                    continue
                if m[0] == 'REQ':
                    sid, filters = m[1], [f for f in m[2:] if isinstance(f, dict)]
                    self.subs[(ws, sid)] = filters
                    self.reqs.append(filters)
                    out = []
                    for f in filters:
                        hits = sorted((e for e in self.events if _match(f, e)), key=lambda e: -e['created_at'])
                        out += [e for e in hits[:f.get('limit') or None] if e not in out]
                    for e in out:
                        await ws.send(json.dumps(['EVENT', sid, e]))
                    await ws.send(json.dumps(['EOSE', sid]))
                elif m[0] == 'CLOSE':
                    self.subs.pop((ws, m[1]), None)
                elif m[0] == 'EVENT' and isinstance(m[1], dict):
                    ev = m[1]
                    self.writes.append(ev)
                    if ev not in self.events:
                        self.events.append(ev)
                    await ws.send(json.dumps(['OK', ev.get('id', ''), True, '']))
                    for (w, sid), filters in list(self.subs.items()):
                        if w is not ws and any(_match(f, ev) for f in filters):
                            try:
                                await w.send(json.dumps(['EVENT', sid, ev]))
                            except websockets.ConnectionClosed:
                                self.subs.pop((w, sid), None)
        except websockets.ConnectionClosed:
            pass
        finally:
            for k in [k for k in self.subs if k[0] is ws]:
                self.subs.pop(k, None)


class Home:
    """THIS node's relay (the shipped RelayServer + PosterChanDB) and its Concord tunnel, on one port."""

    def __init__(self, loop: _Loop, tmp: str, cfg: dict | None = None, members=()):
        from app.services.nostr_relay.pcdb_store import PcdbRelayStore, init_new
        from app.services.nostr_relay.server import RelayServer
        from app.services.nostr_relay.wot import WotGate
        self._loop = loop
        self.tunnel = tunnel_module()
        self.tunnel._open.clear()
        self.tunnel._total = 0
        self.tunnel_peak = 0
        self.tunnel_refused = 0
        self.tunnel_opened = 0
        path = os.path.join(tmp, 'home-relay')
        init_new(path)
        self.store = PcdbRelayStore(path, max_events=0, retention_days=0, maintenance=False)
        loop.run(self._open())
        self.gate = WotGate()
        if members:
            self.gate.add_members(list(members))
        conf = {'wot_enabled': False, 'block_json': False, 'max_subs_per_conn': 64, 'neg_max_items': 50000}
        conf.update(cfg or {})
        self.relay = RelayServer(self.store, self.gate, conf)
        self.server = loop.serve(self._handler, self._pr)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = 'ws://127.0.0.1:%d' % self.port
        self.far = {}          # url (as the client names it) -> FarRelay
        # THE PUBLIC RELAYS A TUNNEL MAY REACH: a real public url (wss://jskitty.com/nostr, ...) -> the local FarRelay
        # that stands in for it. Empty = only loopback targets, as before (Node(internet=...)).
        self.internet = {}

    async def _open(self):
        self.store.open(asyncio.get_running_loop())

    def add(self, events):
        async def go():
            return await self.store.add_events_bulk(list(events), origin='direct')
        return self._loop.run(go())

    def query(self, flt: dict) -> list:
        """What this relay holds that matches `flt` (read back over a real socket, as a client would)."""
        async def go():
            out = []
            async with websockets.connect(self.url, max_size=None) as ws:
                await ws.send(json.dumps(['REQ', 'q', flt]))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if m[0] == 'EVENT':
                        out.append(m[2])
                    elif m[0] in ('EOSE', 'CLOSED'):
                        return out
        return self._loop.run(go())

    def publish(self, ev) -> list:
        """Publish like another person's client: one EVENT over a socket, the relay's OK back."""
        async def go():
            async with websockets.connect(self.url, max_size=None) as ws:
                await ws.send(json.dumps(['EVENT', ev]))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if m[0] == 'OK' and m[1] == ev['id']:
                        return m
        return self._loop.run(go())

    # -------- the tunnel (app/routers/concord_tunnel.py, minus the SSRF check: every relay here is loopback)
    def _pr(self, conn, req):
        path = req.path or '/'
        if path.startswith('/ws/concord-relay'):
            q = dict(x.split('=', 1) for x in path.split('?', 1)[1].split('&') if '=' in x) if '?' in path else {}
            from urllib.parse import unquote
            target, ticket = unquote(q.get('u', '')), unquote(q.get('t', ''))
            if ticket != TICKET:
                return conn.respond(403, 'ticket')
            if not self.tunnel.acquire(TICKET[:64]):
                self.tunnel_refused += 1
                return conn.respond(403, 'too many relay connections')
            setattr(conn, '_pc_tunnel_target', target)
            self.tunnel_opened += 1
            self.tunnel_peak = max(self.tunnel_peak, self.tunnel._open.get(TICKET[:64], 0))
            return None
        return self.relay.process_request(conn, req)

    async def _handler(self, ws):
        target = getattr(ws, '_pc_tunnel_target', None)
        if target is None:
            return await self.relay.handle(ws)
        try:
            n = target.rstrip('/')
            stand_in = self.internet.get(n)
            n = stand_in.url if stand_in is not None else n
            if not re.match(r'^wss?://127\.0\.0\.1:\d+$', n):
                return
            async with websockets.connect(n, max_size=None, open_timeout=10) as up:
                async def down():
                    async for msg in up:
                        await ws.send(msg)

                async def upward():
                    async for msg in ws:
                        await up.send(msg)
                tasks = [asyncio.ensure_future(down()), asyncio.ensure_future(upward())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in tasks:
                        t.cancel()
        except Exception:
            pass
        finally:
            self.tunnel.release(TICKET[:64])

    def close(self):
        try:
            self.server.close()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass


class Foreign:
    """A Blossom server that is not PosterChan's: BUD-01 GET /<sha>, BUD-02 GET /list/<pubkey>, CORS. It has
    never heard of `/thumb/<sha>` (that is our own server's preview route) and answers 404, as real ones do."""

    def __init__(self):
        self.blobs: dict[str, tuple[bytes, str]] = {}
        self.unlisted: set[str] = set()
        self.owner = ''
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Headers', '*')

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                p = self.path.split('?')[0]
                outer.hits.append(self.path)
                m = re.match(r'^/list/([0-9a-f]{64})$', p)
                if m:
                    rows = [{'sha256': s, 'size': len(b), 'type': t, 'uploaded': int(time.time()) - 60,
                             'url': 'http://%s/%s' % (self.headers.get('Host'), s)}
                            for s, (b, t) in outer.blobs.items() if s not in outer.unlisted] if m.group(1) == outer.owner else []
                    data = json.dumps(rows).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                else:
                    m = re.match(r'^/([0-9a-f]{64})(\.[a-z0-9]+)?$', p)
                    if not m or m.group(1) not in outer.blobs:
                        data = b'not found'
                        self.send_response(404)
                    else:
                        data, typ = outer.blobs[m.group(1)]
                        self.send_response(200)
                        self.send_header('Content-Type', typ)
                self.send_header('Content-Length', str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)

            def do_HEAD(self):
                m = re.match(r'^/([0-9a-f]{64})', self.path)
                self.send_response(200 if m and m.group(1) in outer.blobs else 404)
                self._cors()
                self.end_headers()

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d' % self.http.server_address[1]

    def put(self, data: bytes, typ='image/png', owned=True) -> str:
        """Store a blob; `owned` = listed under `owner` (/list), else only fetchable by hash (somebody else's)."""
        sha = hashlib.sha256(data).hexdigest()
        self.blobs[sha] = (data, typ)
        if not owned:
            self.unlisted.add(sha)
        return sha


# ------------------------------------------------------------------------------------------------ the instance
NODE = {'home': None, 'api': {}, 'seen': [], 'config': {}}   # config: extra /client/config keys (game bot npubs, ...)


class Instance(H.Instance):
    def _splice_to(self, port):
        """Byte-for-byte TCP splice of this request to the tunnel front, the way a reverse proxy routes the upgrade."""
        import socket
        import threading
        up = socket.create_connection(('127.0.0.1', port))
        head = '%s %s %s\r\n' % (self.command, self.path, self.request_version)
        head += ''.join('%s: %s\r\n' % (k, v) for k, v in self.headers.items()) + '\r\n'
        up.sendall(head.encode('latin-1'))
        cli = self.connection

        def pump(a, b):
            try:
                while True:
                    d = a.recv(65536)
                    if not d:
                        break
                    b.sendall(d)
            except OSError:
                pass
            finally:
                try:
                    b.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        t = threading.Thread(target=pump, args=(up, cli), daemon=True)
        t.start()
        pump(cli, up)
        t.join(timeout=5)
        up.close()
        self.close_connection = True

    # A bundled app (APK / desktop) calls the instance cross-origin; the real server answers CORS for it.
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', self.headers.get('Origin') or '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS, HEAD')
        super().end_headers()

    def do_GET(self):
        p = self.path.split('?')[0]
        NODE['seen'].append(p)
        home = NODE['home']
        if p == '/ws/concord-relay' and home is not None:
            return self._splice_to(home.port)    # an APP route on a real node (nginx -> 3051), never the relay port
        if p == '/client/config' and home is not None:
            return self._json({'nostr_only': False, 'relay_url': home.url, 'relays': [home.url],
                               'default_relays': [home.url], 'blossom_enabled': True, **NODE['config']})
        if p in NODE['api']:
            v = NODE['api'][p]
            return self._json(v() if callable(v) else v)
        return super().do_GET()


class Node:
    """`with Node(...) as n:` -- Home + far relays + the instance + a foreign Blossom, torn down after."""

    def __init__(self, far=0, cfg=None, members=(), tunnel=True, internet=()):
        """`internet`: public relay urls the TUNNEL reaches, each played by its own FarRelay (`n.internet[url]`) --
        e.g. CORD_PUBLIC, the relays every Concord client also reads (invites, membership vaults, backups)."""
        self.tmp = tempfile.mkdtemp(prefix='pc-realnode-')
        self.loop = _Loop()
        self.home = Home(self.loop, self.tmp, cfg, members)
        self.far = [FarRelay(self.loop, 'far%d' % i) for i in range(far)]
        self.internet = {u.rstrip('/'): FarRelay(self.loop, u) for u in internet}
        self.home.internet = self.internet
        self.foreign = Foreign()
        H.reset()
        H.OVERRIDES.clear()
        for pair in filter(None, os.environ.get('PC_JOURNEY_OVERRIDE', '').split(',')):
            path, _, src = pair.partition('=')
            H.OVERRIDES[path] = src
        if H.EMPTY is None:
            H.EMPTY = H.EmptyRelay().start()
        NODE['home'] = self.home
        NODE['seen'] = []
        NODE['config'] = {}
        NODE['api'] = {'/client/concord/tunnel-ticket': {'ticket': TICKET if tunnel else '', 'ttl': 3600}}
        full.ROOT = ROOT
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Instance)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.port = self.http.server_address[1]
        self.origin = 'http://127.0.0.1:%d' % self.port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.http.shutdown()
            self.http.server_close()
        except Exception:
            pass
        self.home.close()
        for f in self.far + list(self.internet.values()):
            try:
                f.server.close()
            except Exception:
                pass
        try:
            self.foreign.http.shutdown()
            self.foreign.http.server_close()
        except Exception:
            pass
        try:
            self.loop.loop.call_soon_threadsafe(self.loop.loop.stop)
        except Exception:
            pass
        NODE['home'] = None
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------------------------------- a device
PAGE_INIT = H.PAGE_INIT + r'''
window.__toastLog=[];
// Every toast the person saw, in order (the app draws them as #toast-root .toast for ~3 s).
new MutationObserver(ms=>{for(const m of ms)for(const n of m.addedNodes)
  if(n.nodeType===1&&n.classList&&n.classList.contains('toast'))__toastLog.push(String(n.textContent||''));})
  .observe(document,{childList:true,subtree:true});
'''


class Device:
    """One Chrome profile = one phone or one desktop. `async with Device(node, sk_fill, phone=True) as d:`
    signs in by typing an nsec into the login screen and yields `d` (`d.b` is the CDP browser)."""

    def __init__(self, node: Node, sk_fill=1, phone=False, pre_init='', port=None, profile=None, url=None, login=True):
        """`login=False`: stop at the login screen (a device that signs in some other way, e.g. a remote signer)."""
        self.node, self.sk_fill, self.phone, self.login = node, sk_fill, phone, login
        self.url = url or node.origin + '/client'
        self.width, self.height = (390, 844) if phone else (1440, 900)
        self.pre_init = pre_init
        self.port = port
        self.profile = profile

    async def __aenter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pc-realnode-chrome-', ignore_cleanup_errors=True)
        prof = self.profile or self.tmp.name
        port_arg = str(self.port) if self.port else '0'
        self.proc = subprocess.Popen([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
                                      '--remote-debugging-port=' + port_arg, '--user-data-dir=' + prof,
                                      'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cdp = None
        for _ in range(300):
            if self.port:
                cdp = str(self.port)
                try:
                    async with httpx.AsyncClient(trust_env=False) as h:
                        await h.get('http://127.0.0.1:%s/json/version' % cdp, timeout=1)
                    break
                except httpx.HTTPError:
                    pass
            else:
                try:
                    lines = Path(prof, 'DevToolsActivePort').read_text().splitlines()
                    if lines and lines[0].isdigit():
                        cdp = lines[0]
                        break
                except FileNotFoundError:
                    pass
            if self.proc.poll() is not None:
                raise RuntimeError('Chrome exited at start')
            await asyncio.sleep(.1)
        target = None
        async with httpx.AsyncClient(trust_env=False) as h:
            for _ in range(100):
                try:
                    pages = (await h.get('http://127.0.0.1:%s/json' % cdp)).json()
                    target = next((x['webSocketDebuggerUrl'] for x in pages if x.get('type') == 'page'), None)
                    if target:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.1)
        self.ws = await websockets.connect(target, max_size=80_000_000)
        b = self.b = full.Browser(self.ws)
        await b.call('Page.enable')
        await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
        await b.call('Emulation.setDeviceMetricsOverride', dict(
            width=self.width, height=self.height, deviceScaleFactor=1, mobile=self.phone,
            screenWidth=self.width, screenHeight=self.height))
        if self.phone:
            await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
        init = (self.pre_init + PAGE_INIT).replace('__EMPTY__', str(H.EMPTY.port))
        await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': init})
        await b.call('Page.navigate', {'url': self.url})
        await wait(b, '!!window.__PC && !!window.NostrTools && document.readyState==="complete"', 60,
                   'the client shell loaded')
        await wait(b, "document.body.classList.contains('guest') || !!__PC.me()", 60, 'the login screen')
        if not self.login:
            return self
        if not await b.js('!!__PC.me()'):
            await b.js("(()=>{const key=new Uint8Array(32).fill(%d);document.querySelector('#nsec-input').value="
                       "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()"
                       % self.sk_fill)
        await wait(b, '!!__PC.me() && Relay.status==="ok"', 60, 'signed in with the nsec and connected')
        return self

    async def __aexit__(self, *exc):
        try:
            await asyncio.wait_for(self.b.call('Browser.close'), 3)
        except Exception:
            pass
        try:
            await self.ws.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.tmp.cleanup()

    async def toasts(self):
        return await self.b.js('window.__toastLog||[]')

    async def text(self):
        return await self.b.js('document.body.innerText')


class StepFailed(AssertionError):
    pass


async def wait(b, expression, seconds=30, what=None, state=None):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        try:
            if await b.js(expression):
                return
        except AssertionError:
            pass
        await asyncio.sleep(.2)
    try:
        st = await b.js(state or "({toasts:(window.__toastLog||[]).slice(-6),errors:(window.__errors||[]).slice(-6),"
                                 "view:(window.__PC&&__PC.isView&&['home','global','notifications','thread','messages','blossom']"
                                 ".find(v=>__PC.isView(v)))||'',text:document.body.innerText.slice(0,600)})")
    except Exception as e:
        st = 'state unreadable: %s' % e
    raise StepFailed({'expected': what or expression, 'seen': st})


def png(rgb, size=8):
    return base64.b64decode(H.png(rgb, size))


# ------------------------------------------------------------------------------------------- the APK bundle
class Bundle:
    """The APK's web bundle, built by the SHIPPED mobile/build-www.sh into a private tree (the shell plus its
    bundled-mode shim: every root-relative fetch/WebSocket rewritten to `__PC_API_BASE__`), served from its own
    origin the way the WebView serves https://localhost. `with Bundle() as bu:` -> `bu.origin`."""

    def __enter__(self):
        from http.server import SimpleHTTPRequestHandler
        self.tmp = tempfile.mkdtemp(prefix='pc-apk-bundle-')
        src = Path(self.tmp)
        for name in ('static', 'templates', 'scripts', 'app'):
            (src / name).symlink_to(ROOT / name, target_is_directory=True)
        (src / 'mobile').mkdir()
        shutil.copyfile(ROOT / 'mobile/build-www.sh', src / 'mobile/build-www.sh')
        r = subprocess.run(['bash', str(src / 'mobile/build-www.sh')], cwd=src, capture_output=True, text=True,
                           timeout=120)
        if r.returncode:
            raise RuntimeError('mobile/build-www.sh failed: ' + (r.stdout + r.stderr)[-2000:])
        root = src / 'mobile/www'

        class H2(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(root), **kw)

            def log_message(self, *a):
                pass
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), H2)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.origin = 'http://127.0.0.1:%d' % self.http.server_address[1]
        return self

    def __exit__(self, *exc):
        try:
            self.http.shutdown()
            self.http.server_close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def instance_is(origin: str) -> str:
    """pre_init for a bundle: the instance this install was pointed at (Settings / the login screen store it)."""
    return "try{localStorage.setItem('pc_instance',%s)}catch(_){}\n" % json.dumps(origin)


# ------------------------------------------------------------------------------------------------- Concord
# The public relays concord.js reads besides this node (CORD_RELAYS + DISCOVER_RELAYS + the backups
# concord_mirror.BACKUP_RELAYS) -- what `Node(internet=CORD_PUBLIC)` stands in for.
CORD_PUBLIC = ('wss://jskitty.com/nostr', 'wss://asia.vectorapp.io/nostr', 'wss://nostr.computingcache.com',
               'wss://relay.dreamith.to')
MINT_COMMUNITIES = r'''(async(relaySets,names)=>{
  const me=__PC.me().pubkey, rooms=[];
  for(let i=0;i<names.length;i++){
    const relays=relaySets[i%relaySets.length];
    const made=await PosterCord.createCommunity({owner:me,name:names[i],relays,base:'https://cord.invalid',signEvent:__PC.signTemplate});
    const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
    const controls=made.events.filter(e=>e.kind===1059);
    const info=PosterCordReader.inspectControl(bundle,controls);
    rooms.push({name:names[i],communityId:made.communityId,naddr:'cord-'+i,url:made.url,
      channels:info.channels.map(c=>({name:c.name,id:c.id,private:!!c.private})),cord:{bundle},__events:made.events});
  }
  return rooms;
})'''


async def seed_communities(n, d, count=16):
    """`count` communities this account belongs to, hosted on relays ELSEWHERE (n.far, never this node), their
    genesis on those relays -- a member who has joined a few dozen rooms over the months. Minted by the shipped
    PosterCord in the signed-in page, kept in the account's room list, then the app is started again."""
    b = d.b
    await b.js("__PC.switchMessagesTab('concord')")
    await wait(b, '!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord', 40, 'Concord loaded')
    far = [f.url for f in n.far]
    sets = [[far[0], far[1]], [far[1], far[2]], [far[2], far[3]], [far[3], far[0], far[1]]]
    rooms = await b.js(MINT_COMMUNITIES + '(%s,%s)' % (json.dumps(sets), json.dumps(['Room %d' % i for i in range(count)])))
    for r in rooms:
        evs = r.pop('__events')
        for f in n.far:
            if f.url in r['cord']['bundle']['relays']:
                f.events.extend(evs)
    await b.js("localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,%s);"
               "localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');true"
               % json.dumps(json.dumps(rooms)))
    await b.call('Page.reload')
    await wait(b, '!!window.__PC && !!__PC.me && !!__PC.me() && Relay.status==="ok"', 60, 'signed in again')
    return rooms


async def open_room_channel(d, channel='general'):
    """Concord -> the active community -> #channel, as a person taps it (a phone opens on the channel list)."""
    b = d.b
    await b.js("__PC.switchMessagesTab('concord')")
    sel = "[data-cc-channel=%s]" % json.dumps(channel)
    await wait(b, "!!document.querySelector(%s)" % json.dumps(sel), 40, 'the community lists #' + channel)
    await b.js("(()=>{if(!document.querySelector('.cc-app.show-chat'))document.querySelector(%s).click();return true})()"
               % json.dumps(sel))
    await wait(b, "!!document.querySelector('#cc-input') && !!document.querySelector('#cc-send')"
                  " && document.querySelector('#cc-input').getClientRects().length>0", 40,
               'the Concord composer in #' + channel)


async def concord_send(d, text):
    b = d.b
    await b.js("(()=>{const el=document.querySelector('#cc-input');el.focus();el.value=%s;"
               "el.dispatchEvent(new Event('input',{bubbles:true}));})()" % json.dumps(text))
    await b.js("document.querySelector('#cc-send').click()")
