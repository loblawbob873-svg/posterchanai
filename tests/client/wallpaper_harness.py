"""A REAL relay and a REAL instance (files index + Blossom), for driving the shipped web client.

The desktop wallpaper crosses four boundaries -- the encrypted drive index (`/client/files-index`),
Blossom (`/blossom/<sha>`), the account's encrypted layout on the relay (`pcai:desktop`) and the live
subscription that carries it to another device -- and an in-page stub of any one of them is the
place a bug can hide. So these are real servers, shared by every browser a test starts: a second
Chrome PROFILE is a second device with nothing cached, and what it sees it had to read back.

Knobs (`STATE`), each a way the user's own layout read or write can go wrong:
  hold_layout  None | 'late' | 'never' -- a REQ for pcai:desktop is held (answered on release()) or
               never answered at all (no EVENT, no EOSE)
  refuse_layout  publish of pcai:desktop is answered OK=false
  hold_docs    a set of d-tags (an entry ending in ':' is a prefix) or l-tags: a REQ naming one is never
               answered by THIS relay either -- with the SilentRelay in the pool, nobody answers it
  api          url path -> JSON the instance answers with (an endpoint a scenario needs, e.g. Go Live)
Every kind-30078 event the relay is SENT is kept in STATE['pubs'], accepted or not.
"""
import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import websockets

from tests.client import test_effects_full_app as full

ROOT = Path(__file__).resolve().parents[2]
CHROME = '/opt/google/chrome/chrome'

STATE = {}
OVERRIDES = {}      # url path -> local file served in its place


def reset():
    STATE.clear()
    STATE.update(events=[], blobs={}, index=None, hold_layout=None, refuse_layout=False,
                 held=[], layout_pubs=[], hold_docs=set(), pubs=[], api={}, lock=threading.Lock(),
                 held_open={}, held_seen=0, held_closed_at=None)


reset()


def _d(ev):
    return next((t[1] for t in ev.get('tags', []) if t and t[0] == 'd' and len(t) > 1), '')


def _is_layout(ev):
    return ev.get('kind') == 30078 and _d(ev) == 'pcai:desktop'


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


def _layout_req(filters):
    return any('pcai:desktop' in (f.get('#d') or []) for f in filters)


def _held_doc_req(filters):
    """Does this REQ ask for a document `hold_docs` names (by d-tag, d-prefix or l-tag)?"""
    held = STATE.get('hold_docs') or ()
    for f in filters:
        for d in (f.get('#d') or []) + (f.get('#l') or []):
            if any(d == h or (h.endswith(':') and str(d).startswith(h)) for h in held):
                return True
    return False


def _dmatch(d):
    return (lambda x: x.startswith(d)) if d.endswith(':') else (lambda x: x == d)


def docs(d):
    """Every STORED kind-30078 event of this d-tag (or d-prefix ending in ':'), newest first."""
    m = _dmatch(d)
    return sorted((e for e in STATE['events'] if e.get('kind') == 30078 and m(_d(e))),
                  key=lambda e: -e['created_at'])


def pubs(d):
    """Every kind-30078 event of this d-tag (or prefix) the relay was SENT, accepted or not."""
    m = _dmatch(d)
    return [e for e in STATE['pubs'] if m(_d(e))]


class Relay:
    """A small NIP-01 relay: stores, replaces addressable events, answers REQ, forwards live."""

    def __init__(self):
        self.subs = {}          # (ws, subid) -> filters
        self.loop = None
        self.port = None

    def _store(self, ev):
        with STATE['lock']:
            evs = STATE['events']
            if 30000 <= ev['kind'] < 40000 or ev['kind'] in (0, 3) or 10000 <= ev['kind'] < 20000:
                same = [x for x in evs if x['kind'] == ev['kind'] and x['pubkey'] == ev['pubkey']
                        and (_d(x) == _d(ev) if 30000 <= ev['kind'] < 40000 else True)]
                # Newer wins; on an EQUAL created_at the later write wins -- the node relay's
                # `tie_direct` rule for what a client publishes directly (nostr_relay/store.py).
                for x in same:
                    if x['created_at'] > ev['created_at']:
                        return False
                for x in same:
                    evs.remove(x)
            evs.append(ev)
            return True

    async def answer(self, ws, subid, filters):
        out = []
        with STATE['lock']:
            for f in filters:
                hits = sorted((e for e in STATE['events'] if _match(f, e)), key=lambda e: -e['created_at'])
                if f.get('limit'):
                    hits = hits[:f['limit']]
                out += [e for e in hits if e not in out]
        try:
            for e in out:
                await ws.send(json.dumps(['EVENT', subid, e]))
            await ws.send(json.dumps(['EOSE', subid]))
        except websockets.ConnectionClosed:
            pass

    async def handler(self, ws):
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, list) or not m:
                    continue
                if m[0] == 'REQ':
                    subid, filters = m[1], [f for f in m[2:] if isinstance(f, dict)]
                    self.subs[(ws, subid)] = filters
                    hold = STATE['hold_layout'] if _layout_req(filters) else None
                    if _held_doc_req(filters):
                        hold = 'never'
                    if hold == 'late':
                        STATE['held'].append((ws, subid, filters))
                    elif hold == 'never':
                        # A read this relay will never answer: remember it, so a test can tell when the
                        # client has GIVEN UP on it (it closes the subscription) instead of sleeping a guess.
                        STATE.setdefault('held_open', {})[(id(ws), subid)] = __import__('time').monotonic()
                        STATE['held_seen'] = STATE.get('held_seen', 0) + 1
                    else:
                        await self.answer(ws, subid, filters)
                elif m[0] == 'CLOSE':
                    self.subs.pop((ws, m[1]), None)
                    if STATE.get('held_open', {}).pop((id(ws), m[1]), None) is not None:
                        STATE['held_closed_at'] = __import__('time').monotonic()
                elif m[0] == 'EVENT':
                    ev = m[1]
                    if isinstance(ev, dict) and ev.get('kind') == 30078:
                        STATE['pubs'].append(ev)
                    if _is_layout(ev):
                        STATE['layout_pubs'].append(ev)
                        if STATE['refuse_layout']:
                            await ws.send(json.dumps(['OK', ev['id'], False, 'blocked: fixture refuses']))
                            continue
                    self._store(ev)
                    await ws.send(json.dumps(['OK', ev['id'], True, '']))
                    for (w, sid), filters in list(self.subs.items()):
                        if any(_match(f, ev) for f in filters):
                            try:
                                await w.send(json.dumps(['EVENT', sid, ev]))
                            except websockets.ConnectionClosed:
                                self.subs.pop((w, sid), None)
        except websockets.ConnectionClosed:
            pass
        finally:
            for k in [k for k in self.subs if k[0] is ws]:
                self.subs.pop(k, None)

    def start(self):
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            async def main():
                server = await websockets.serve(self.handler, '127.0.0.1', 0, max_size=None)
                self.port = server.sockets[0].getsockname()[1]
                ready.set()
                await asyncio.Future()
            self.loop.run_until_complete(main())
        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
        return self

    def release(self):
        """Answer every held layout read now."""
        held, STATE['held'] = STATE['held'], []
        STATE['hold_layout'] = None
        for ws, subid, filters in held:
            asyncio.run_coroutine_threadsafe(self.answer(ws, subid, filters), self.loop)
        return len(held)


class SilentRelay(Relay):
    """A second relay in the user's own list -- a public relay that is up, accepts what it is sent and
    answers the timeline, but never answers a private-document read (it dropped the REQ under load,
    or it is waiting on an AUTH nobody will give). One of these is all a "use my own relays" pool
    needs for `Relay.query()` to come back `complete:false` on every attempt."""

    async def handler(self, ws):
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, list) or not m:
                    continue
                if m[0] == 'REQ':
                    filters = [f for f in m[2:] if isinstance(f, dict)]
                    if any(30078 in (f.get('kinds') or []) for f in filters):
                        continue
                    await ws.send(json.dumps(['EOSE', m[1]]))
                elif m[0] == 'EVENT':
                    await ws.send(json.dumps(['OK', m[1]['id'], True, '']))
        except websockets.ConnectionClosed:
            pass


class EmptyRelay(Relay):
    """Stands in for every relay OFF this machine (see LOOPBACK_ONLY): up, answers every REQ with an
    EOSE and nothing else, takes every EVENT and keeps none -- a reachable public relay that holds
    nothing of this account's."""

    async def handler(self, ws):
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, list) or not m:
                    continue
                if m[0] == 'REQ':
                    await ws.send(json.dumps(['EOSE', m[1]]))
                elif m[0] == 'EVENT' and isinstance(m[1], dict):
                    await ws.send(json.dumps(['OK', m[1].get('id', ''), True, '']))
        except websockets.ConnectionClosed:
            pass


RELAY = None
SILENT = None
EMPTY = None


class Instance(full.Handler):
    """The web client's own server: the shell, its static files, the drive index and Blossom."""

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def do_GET(self):
        p = self.path.split('?')[0]
        if p in OVERRIDES:                  # serve an older copy of one shipped file (fail-first runs)
            data = Path(OVERRIDES[p]).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == '/client/config':
            url = f'ws://127.0.0.1:{RELAY.port}'
            return self._json({'nostr_only': False, 'relay_url': url, 'relays': [url], 'default_relays': [url],
                               'blossom_enabled': True})
        if p == '/client/blossom-access':
            return self._json({'allowed': True, 'whitelisted': True})
        if p in STATE.get('api', {}):
            return self._json(STATE['api'][p])
        m = re.match(r'^/blossom/([0-9a-f]{64})(\.[a-z0-9]+)?$', p)
        if m:
            data = STATE['blobs'].get(m.group(1))
            if data is None:
                return self._json({'error': 'not found'}, 404)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p.startswith('/api/') or (p.startswith('/client/') and not p.endswith('.js') and p != '/client'):
            return self._json({})
        return super().do_GET()

    def do_HEAD(self):
        m = re.match(r'^/blossom/([0-9a-f]{64})', self.path)
        if m:
            self.send_response(200 if m.group(1) in STATE['blobs'] else 404)
            self.end_headers()
            return
        return super().do_HEAD()

    def do_PUT(self):
        if self.path.split('?')[0] == '/blossom/upload':
            data = self._body()
            sha = hashlib.sha256(data).hexdigest()
            STATE['blobs'][sha] = data
            origin = f'http://{self.headers.get("Host")}'
            return self._json({'url': f'{origin}/blossom/{sha}', 'sha256': sha, 'size': len(data),
                               'type': self.headers.get('Content-Type') or ''})
        return self._json({}, 404)

    def do_POST(self):
        p = self.path.split('?')[0]
        body = self._body()
        if p == '/client/files-index':
            try:
                j = json.loads(body or b'{}')
            except ValueError:
                j = {}
            if j.get('index') is not None:
                STATE['index'] = j['index']
                return self._json({'ok': True, 'mk': j['index'].get('mk')})
            return self._json({'ok': True, 'index': STATE['index']})
        return self._json({})

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()


HTTP = None


def start():
    global RELAY, SILENT, EMPTY, HTTP
    reset()
    if RELAY is None:
        RELAY = Relay().start()
    if SILENT is None:
        SILENT = SilentRelay().start()
    if EMPTY is None:
        EMPTY = EmptyRelay().start()
    if HTTP is None:
        full.ROOT = ROOT
        HTTP = ThreadingHTTPServer(('127.0.0.1', 0), Instance)
        threading.Thread(target=HTTP.serve_forever, daemon=True).start()
    return HTTP.server_address[1]


# NOTHING LEAVES THE MACHINE. `Network.setBlockedURLs` does not apply to WebSockets, and the client's own
# fallback relay (wss://relay.poster.place, app.js FALLBACK_RELAYS) joins every "use my own relays" pool --
# measured: that pool's reads were answered by PRODUCTION (it holds documents earlier runs published for
# this fixed test key) and its writes went there too. Any socket not on loopback is pointed at the local
# EmptyRelay instead -- a reachable relay holding nothing. (A CLOSED port was tried first: the client's
# reconnect handling of a dozen failing discovery sockets then cost the round-trip test its live update.)
LOOPBACK_ONLY = r'''
(()=>{const W=window.WebSocket;if(!W||W.__pcLoopback)return;
  const L=function(u,p){if(!/^wss?:\/\/(127\.0\.0\.1|localhost)[:/]/i.test(String(u)))u='ws://127.0.0.1:__EMPTY__/';
    return p===undefined?new W(u):new W(u,p);};
  L.prototype=W.prototype;L.CONNECTING=0;L.OPEN=1;L.CLOSING=2;L.CLOSED=3;L.__pcLoopback=true;window.WebSocket=L;})();
'''

PAGE_INIT = full.FIRST_RUN_DONE + LOOPBACK_ONLY + r'''
window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message||e)));
window.__toasts=[];
'''

TOASTS = r'''(()=>{if(window.__toastHooked)return true;window.__toastHooked=true;const t=__PC.toast;
  __PC.toast=function(m){__toasts.push(String(m));return t&&t.apply(this,arguments)};return true})()'''


class Device:
    """One Chrome profile = one device. `async with Device() as b:` yields a signed-in Browser."""

    def __init__(self, sk_fill=1, width=1440, height=900, own_relays=False):
        self.sk_fill, self.width, self.height = sk_fill, width, height
        self.own_relays = own_relays

    async def __aenter__(self):
        port = HTTP.server_address[1]
        self.tmp = tempfile.TemporaryDirectory(prefix='pc-wallpaper-', ignore_cleanup_errors=True)
        prof = self.tmp.name
        self.proc = subprocess.Popen([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
                                      '--remote-debugging-port=0', '--user-data-dir=' + prof, 'about:blank'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(300):
            try:
                lines = Path(prof, 'DevToolsActivePort').read_text().splitlines()
                if lines and lines[0].isdigit():
                    break
            except FileNotFoundError:
                pass
            await asyncio.sleep(.1)
        cdp = Path(prof, 'DevToolsActivePort').read_text().splitlines()[0]
        target = None
        async with httpx.AsyncClient(trust_env=False) as h:
            for _ in range(100):
                try:
                    pages = (await h.get(f'http://127.0.0.1:{cdp}/json')).json()
                    target = next((x['webSocketDebuggerUrl'] for x in pages if x.get('type') == 'page'), None)
                    if target:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.1)
        self.ws = await websockets.connect(target, max_size=50_000_000)
        b = self.b = full.Browser(self.ws)
        await b.call('Page.enable')
        if not self.own_relays or self.own_relays is True:
            await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=self.width, height=self.height,
                     deviceScaleFactor=1, mobile=False, screenWidth=self.width, screenHeight=self.height))
        init = PAGE_INIT.replace('__EMPTY__', str(EMPTY.port))
        if self.own_relays:
            # "Use my own relays" ON, with a list holding one more relay than the node's: the pool is
            # then the user's list + the node's relay (connectRelays), as on a real account.
            mine = self.own_relays if isinstance(self.own_relays, list) else [f'ws://127.0.0.1:{SILENT.port}']
            init += ("try{const s=JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}');"
                     "s.relaysEnabled=true;s.relaySwitchAsked='mine';s.relays=%s;"
                     "localStorage.setItem('pc_nostr_settings',JSON.stringify(s));}catch(_){}\n"
                     % json.dumps(mine))
        await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': init})
        await b.call('Page.navigate', {'url': f'http://127.0.0.1:{port}/client'})
        await wait(b, '!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
        await wait(b, "document.body.classList.contains('guest') || !!__PC.me()")
        if not await b.js('!!__PC.me()'):
            await b.js("(()=>{const key=new Uint8Array(32).fill(%d);document.querySelector('#nsec-input').value="
                       "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()"
                       % self.sk_fill)
        await wait(b, '!!__PC.me() && Relay.status==="ok"')
        await b.js(TOASTS)
        return b

    async def reload(self):
        await self.b.call('Page.reload')
        await wait(self.b, '!!window.__PC && !!__PC.me && !!__PC.me() && Relay.status==="ok"')
        await self.b.js(TOASTS)

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


async def wait(b, expression, seconds=40, state=None):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(10, seconds)
    while loop.time() < deadline:
        try:
            if await b.js(expression):
                return
        except AssertionError:
            pass
        await asyncio.sleep(.2)
    raise AssertionError({'waiting': expression, 'state': await b.js(state or
        "({errors:window.__errors,toasts:window.__toasts,"
        "desk:(document.querySelector('#os-desk')||{}).className,"
        "bg:((document.querySelector('#os-desk')||{}).style||{}).backgroundImage})")})


PROMPT_OK = r"""(v=>{const i=document.querySelector('.uiconfirm-bg .uiprompt-in');i.value=v;
  i.dispatchEvent(new Event('input',{bubbles:true}));
  document.querySelector('.uiconfirm-bg [data-uc="1"]').click();return true})"""


async def answer_prompt(b, value, seconds=20):
    """Type into the app's own prompt dialog (uiPrompt) and press its OK button, as a person does."""
    await wait(b, "!!document.querySelector('.uiconfirm-bg .uiprompt-in')", seconds)
    await b.js(PROMPT_OK + '(%s)' % json.dumps(value))


def png(rgb, size=4):
    """A real, decodable PNG of one colour, base64 -- so an <img> or a CSS background can paint it."""
    import base64
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    raw = b''.join(b'\x00' + bytes(rgb) * size for _ in range(size))
    return base64.b64encode(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
                            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')).decode()


PNG_RED = png((220, 30, 30))
PNG_BLUE = png((30, 60, 220))


async def put_picture(b, name, folder='Backgrounds', encrypted=True, b64=PNG_RED, mime='image/png'):
    """Put a picture in the drive the way Files does: encrypted (uploadEncFile) or public (uploadBlob),
    then save the index to the instance. Returns the sha the index records."""
    return await b.js("""(async()=>{
      const FI=__PC.filesIdx(); await FI.ensure();
      const bin=Uint8Array.from(atob(%s),c=>c.charCodeAt(0));
      const file=new File([bin],%s,{type:%s});
      if(!FI.folders().includes(%s)) FI.addFolder(%s, %s);
      let sha;
      if(%s){ sha=await __PC.uploadEncFile(file,%s); }
      else { const st={}; const url=await __PC.uploadBlob(file,{hashOut:st,noCompress:true});
             sha=st.sha; FI.setFile(sha,{name:file.name,folder:%s,mime:file.type,size:file.size,ts:Math.floor(Date.now()/1000)}); }
      await FI._save(); return sha; })()""" % (
        json.dumps(b64), json.dumps(name), json.dumps(mime), json.dumps(folder), json.dumps(folder),
        'true' if encrypted else 'false', 'true' if encrypted else 'false', json.dumps(folder),
        json.dumps(folder)))


DESK_UP = "document.body.classList.contains('os-on') && !!document.querySelector('#os-desk .os-icons')"
SHOWN = ("(()=>{const d=document.querySelector('#os-desk');if(!d)return '';"
         "return d.classList.contains('has-bg')?(d.style.backgroundImage||''):''})()")


async def enter_desktop(b):
    await b.js("PCOS.enter();true")
    await wait(b, DESK_UP)


async def open_picker(b):
    """Right-click the empty desktop → Change background…, as a person does."""
    await b.js("(()=>{const d=document.querySelector('#os-desk');const r=d.getBoundingClientRect();"
               "d.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,"
               "clientX:r.right-40,clientY:r.bottom-40}));return true})()")
    await wait(b, "[...document.querySelectorAll('.os-ctx button, .os-ctx .os-ctx-b')]"
                  ".some(x=>/Change background/.test(x.textContent))")
    await b.js("[...document.querySelectorAll('.os-ctx button, .os-ctx .os-ctx-b')]"
               ".find(x=>/Change background/.test(x.textContent)).click()")
    await wait(b, "!!document.querySelector('.os-bgpick .os-bg-item, .os-bgpick .os-bg-empty p')")


async def pick(b, sha):
    await wait(b, "!!document.querySelector('.os-bgpick .os-bg-item[data-sha=\"%s\"]')" % sha,
               state="({toasts:__toasts,picker:(document.querySelector('.os-bgpick')||{}).innerText})")
    await b.js("document.querySelector('.os-bgpick .os-bg-item[data-sha=\"%s\"]').click()" % sha)


BARE_DESK = r'''(()=>{const d=document.querySelector('#os-desk');if(!d)return null;const r=d.getBoundingClientRect();
  for(let y=r.bottom-30;y>r.top+30;y-=37)for(let x=r.right-30;x>r.left+30;x-=41){
    const e=document.elementFromPoint(x,y);if(e===d)return [Math.round(x),Math.round(y)];}
  return null})()'''


async def colour_at(b, x=None, y=None):
    """What the person SEES on a bare patch of the desktop (nothing drawn over it): a screenshot
    pixel, (r, g, b)."""
    import base64
    import io
    from PIL import Image
    if x is None:
        pt = await b.js(BARE_DESK)
        if not pt:
            return (-1, -1, -1)
        x, y = pt
    shot = await b.call('Page.captureScreenshot', {'format': 'png', 'clip': {'x': x, 'y': y, 'width': 1,
                                                                             'height': 1, 'scale': 1}})
    return Image.open(io.BytesIO(base64.b64decode(shot['data']))).convert('RGB').getpixel((0, 0))


def is_red(c):
    return c[0] > 110 and c[1] < 70 and c[2] < 70


def is_blue(c):
    return c[2] > 110 and c[0] < 70 and c[1] < 90


def is_default(c):
    """The default emblem desktop: a real pixel that is neither test picture."""
    return c[0] >= 0 and not is_red(c) and not is_blue(c)


async def sees(b, test, seconds=30):
    """Wait until the desktop pixel satisfies `test` (is_red / is_blue / a lambda)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    c = None
    while loop.time() < deadline:
        c = await colour_at(b)
        if test(c):
            return c
        await asyncio.sleep(.3)
    raise AssertionError({'desktop pixel': c, 'wanted': test.__name__, 'toasts': await b.js('__toasts'),
                          'shown': (await b.js(SHOWN))[:40], 'errors': await b.js('__errors')})


async def seed_layout(b, doc=None, content=None, ahead=0):
    """Store a pcai:desktop on the relay signed with this account's key -- another device's save.
    `doc` is encrypted to the account; `content` is used verbatim (e.g. one that will not decrypt)."""
    ev = await b.js("""(async()=>{const pk=__PC.me().pubkey;
        const content=%s!==null?%s:await __PC.nip44enc(pk,%s);
        return NostrTools.finalizeEvent({kind:30078,created_at:Math.floor(Date.now()/1000)+%d,
            tags:[['d','pcai:desktop']],content},new Uint8Array(32).fill(%d))})()""" % (
        json.dumps(content), json.dumps(content), json.dumps(json.dumps(doc)), ahead, 1))
    RELAY._store(ev)
    return ev


def layout_docs():
    return [e for e in STATE['events'] if _is_layout(e)]


async def decoded_layout(b):
    """The account's pcai:desktop as the RELAY holds it, decrypted with the page's key."""
    docs = layout_docs()
    if not docs:
        return None
    ev = max(docs, key=lambda e: e['created_at'])
    return await b.js("(async()=>JSON.parse(await __PC.nip44dec(__PC.me().pubkey,%s)))()" % json.dumps(ev['content']))



async def gave_up(cap, margin=4.0):
    """Wait until the client has abandoned every read this relay is holding (it CLOSEd them all, after at least
    one), and then `margin` more seconds with no new held read -- the window in which a wrongly-armed writer would
    publish. Never longer than `cap`, the flat wait this replaces, so a client that keeps a read open for ever
    still gets the full window. Measured: the silent-relay tests spent 135s of fixed sleeps."""
    import asyncio
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < cap:
        seen, opened = STATE.get('held_seen', 0), STATE.get('held_open', {})
        closed = STATE.get('held_closed_at')
        if seen and not opened and closed and time.monotonic() - closed >= margin:
            return time.monotonic() - t0
        await asyncio.sleep(.25)
    return cap
