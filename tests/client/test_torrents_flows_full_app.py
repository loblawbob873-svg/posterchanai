"""Torrents -> Downloads, driven end to end in the shipped client against the node's REAL torrent router, with the
engine behind it answering, refusing, down, or never answering.

Topology, as deployed: the node a member talks to (server1) has `bt_server_url` set and FORWARDS every
/api/torrent/* call to another node that runs the libtorrent engine (nas). That forward is the API boundary the
engine is faked at: `app/routers/torrent.py` is mounted in-process exactly as shipped (only `get_torrent_user`,
which is auth and membership, is answered for the test accounts), and `bt_server_url` names an `Engine` in this
process that speaks the engine node's /api/torrent/{list,add,remove,pause,resume}. Each scenario has its own
account and its own engine (wallpaper_harness + run_scenarios: a real relay, a real instance, real Chrome, three
at a time).

  a_torrent_is_added_downloads_and_removing_it_sticks
      Add torrent (torrents.js's dialog) -> the magnet reaches the engine and the row appears; its progress bar
      MOVES across polls; another device adds a torrent at the FRONT of the engine's list (every position shifts)
      and Remove on our row still removes OUR torrent, files kept as answered; the row is gone, and still gone
      after a reload.
  the_downloads_screen_says_what_is_wrong_and_recovers
      An engine that is down is "couldn't reach", never "this server has no torrent client" (that one sends the
      user to an admin about a setting that is already on); when it comes back the list comes back without a
      reload; an engine that never answers ends in a sentence within seconds, not a spinner; a node whose engine
      really has no client says so; and nothing is ever POSTed to the engine on any of it.

The real engine's half of "removed stays removed" (resume files across a restart) is
tests/test_torrent_removal_survives_restart_real_engine.py.

Fail-first: PC_WALLPAPER_OVERRIDE="/static/js/client/discover.js=/tmp/x.js" serves another copy.
"""
import asyncio
import json
import socket
import threading
import time
from contextvars import ContextVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from tests.client import wallpaper_harness as H
from tests.client.test_private_docs_save_with_a_silent_relay_full_app import no_errors, run_scenarios

CHROME = pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required')
FEATURE_COVERS = ('torrents',)
SCENARIOS = {}
FIRST_FILL = 150            # 150, 151 -- no other suite's accounts
COST = {'the_downloads_screen_says_what_is_wrong_and_recovers': 45,
        'a_torrent_is_added_downloads_and_removing_it_sticks': 30}

KEEP = 'a' * 40
NEW = 'b' * 40
OTHER = 'c' * 40


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def _pubkey(fill):
    import coincurve
    return coincurve.PrivateKey(bytes([fill]) * 32).public_key.format(True)[1:].hex()


def _closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------------------------------- the engine node

class Engine:
    """The engine node's /api/torrent/* (what `forward_to_remote` calls), with a list that downloads.

    `mode`: 'ok' | 'down' (the node is pointed at a closed port) | 'hang' (accepts, never answers in time) |
    'off' (answers as a node with no torrent client: 503 "Torrent client not configured")."""

    def __init__(self):
        self.mode, self.torrents, self.calls = 'ok', [], []
        self.dead = _closed_port()
        eng = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, status=200):
                data = json.dumps(obj).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def _do(self, method):
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(n) or b'{}') if n else {}
                path = urlparse(self.path).path.replace('/api/torrent', '')
                eng.calls.append((method, path, body, self.headers.get('X-Posterchanai-Load-Balanced')))
                if eng.mode == 'hang':
                    time.sleep(40)
                    return
                if eng.mode == 'off':
                    return self._send({'detail': 'Torrent client not configured'}, 503)
                status, out = eng.answer(method, path, body)
                self._send(out, status)

            def do_GET(self):
                self._do('GET')

            def do_POST(self):
                self._do('POST')
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % (self.dead if self.mode == 'down' else self.http.server_address[1])

    def add(self, ih, name, progress=0.0, front=False):
        t = {'info_hash': ih, 'name': name, 'size': 1_000_000_000, 'downloaded': 0, 'uploaded': 0,
             'progress': progress, 'download_rate': 0, 'upload_rate': 0, 'state': 'downloading', 'seeders': 3,
             'peers': 5, 'is_paused': False, 'is_finished': False, 'save_path': '/raid/video'}
        self.torrents.insert(0, t) if front else self.torrents.append(t)
        return t

    def hashes(self):
        return [t['info_hash'] for t in self.torrents]

    def posts(self):
        return [(p, b) for m, p, b, _ in self.calls if m == 'POST']

    def answer(self, method, path, body):
        if method == 'GET' and path == '/list':
            for t in self.torrents:                 # every look at the list is a little further along
                if not t['is_paused'] and t['progress'] < 1:
                    t['progress'] = round(min(1.0, t['progress'] + 0.07), 4)
                    t['downloaded'] = int(t['size'] * t['progress'])
                    t['download_rate'] = 2_500_000
            return 200, {'torrents': [dict(t, num=i + 1) for i, t in enumerate(self.torrents)]}
        if method == 'POST' and path == '/add':
            q = parse_qs(urlparse(body.get('magnet') or '').query)
            ih = (q.get('xt') or [''])[0].rsplit(':', 1)[-1].lower()
            if len(ih) != 40:
                return 400, {'detail': 'Invalid magnet link'}
            self.add(ih, (q.get('dn') or [''])[0] or None)
            return 200, {'info_hash': ih, 'message': 'Torrent added', 'selection': ''}
        if method == 'POST' and path in ('/remove', '/pause', '/resume'):
            num = int(body.get('num') or 0)
            if not 1 <= num <= len(self.torrents):
                return 404, {'detail': 'Torrent not found'}
            if path == '/remove':
                self.torrents.pop(num - 1)
            else:
                self.torrents[num - 1]['is_paused'] = path == '/pause'
            return 200, {'message': 'ok'}
        return 404, {'detail': 'Not Found'}


# ------------------------------------------------------------------------------------------- the node

CUR = ContextVar('pc_test_account', default='')


class Node:
    """server1: the REAL app/routers/torrent.py, in this process, forwarding to each account's Engine."""

    def __init__(self, pubkeys):
        self.users = {pk: SimpleNamespace(id=3000 + i, username='t%d' % i, nostr_npub=pk, is_admin=False,
                                          can_torrent=True) for i, pk in enumerate(pubkeys)}
        self.engines = {pk: Engine() for pk in pubkeys}
        self.posts = {pk: [] for pk in pubkeys}       # every POST the client made to /api/torrent/*
        self.loop = self.client = None

    def start(self, mp):
        import httpx
        from fastapi import FastAPI, HTTPException, Request
        from app.database import get_db
        from app.routers import torrent
        from app.services import settings_store

        real_get = settings_store.get
        engines = self.engines

        def get(key, *a, **k):
            if key == 'bt_server_url':
                return engines[CUR.get()].url if CUR.get() in engines else ''
            if key == 'lb_shared_secret':
                return ''
            return real_get(key, *a, **k)
        mp.setattr(settings_store, 'get', get)

        app = FastAPI()
        app.include_router(torrent.router)

        def who(request: Request):
            tok = (request.headers.get('authorization') or '').removeprefix('Bearer ').strip()
            if not tok.startswith('tok-') or tok[4:] not in self.users:
                raise HTTPException(401, 'not signed in')
            return self.users[tok[4:]]
        app.dependency_overrides[torrent.get_torrent_user] = who
        app.dependency_overrides[get_db] = lambda: None
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://node',
                                            timeout=90)
            ready.set()
            self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
        H.ROUTES.append(self.route)

    def stop(self):
        try:
            H.ROUTES.remove(self.route)
        except ValueError:
            pass
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def route(self, h, method, path, body):
        p = path.split('?')[0]
        if p == '/api/auth/nostr-login' and method == 'POST':
            pk = (json.loads(body or b'{}') or {}).get('pubkey') or ''
            if pk not in self.users:
                return False
            u = self.users[pk]
            h._json({'access_token': 'tok-' + pk, 'token_type': 'bearer',
                     'user': {'id': u.id, 'username': u.username, 'nostr_npub': pk, 'is_admin': False}})
            return True
        if p == '/api/instance-welcome/access':
            tok = (h.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
            if tok[4:] not in self.users:
                h._json({'detail': 'not signed in'}, 403)
            else:
                h._json({'pubkey': tok[4:], 'qualified': True})
            return True
        if not p.startswith('/api/torrent/'):
            return False
        headers = {k: v for k, v in h.headers.items() if k.lower() in ('authorization', 'content-type', 'accept')}
        tok = (h.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
        if method == 'POST' and tok[4:] in self.posts:
            self.posts[tok[4:]].append(p)

        async def call():
            CUR.set(tok[4:])
            return await self.client.request(method, path, headers=headers, content=body)
        try:
            r = asyncio.run_coroutine_threadsafe(call(), self.loop).result(90)
        except Exception:
            return True
        try:
            h.send_response(r.status_code)
            h.send_header('Content-Type', r.headers.get('content-type', 'application/json'))
            h.send_header('Content-Length', str(len(r.content)))
            h.end_headers()
            h.wfile.write(r.content)
        except OSError:
            pass                                    # the browser gave up on it first
        return True


NODE = None


@pytest.fixture(scope='module')
def ran(request):
    global NODE
    H.start()
    NODE = Node([_pubkey(FIRST_FILL + i) for i in range(len(SCENARIOS))])
    with pytest.MonkeyPatch.context() as mp:
        NODE.start(mp)
        try:
            check = run_scenarios(request, SCENARIOS, COST, FIRST_FILL)
        finally:
            NODE.stop()
    return check


LIST = "((document.querySelector('#tm-list')||{}).innerText||'')"
ROWS = "[...document.querySelectorAll('#tm-list .tm-item')].map(r=>r.dataset.h)"


async def until(b, expr, seconds=30):
    await H.wait(b, expr, seconds, state="({errors:__errors,toasts:__toasts,list:%s,rows:%s,"
                 "feed:((document.querySelector('#feed')||{}).innerText||'').slice(0,400)})" % (LIST, ROWS))


async def click(b, sel):
    await until(b, "!!document.querySelector(%s)" % json.dumps(sel))
    await b.js("document.querySelector(%s).click();true" % json.dumps(sel))


async def open_downloads(b):
    await b.js("__PC.switchView('torrents');true")
    await until(b, "!!document.querySelector('#tm-list') && !!document.querySelector('.tor-tabs .ntab[data-tt=dl].on')")


def _pct(h):
    return ("(()=>{const r=document.querySelector('#tm-list .tm-item[data-h=\"%s\"] .tm-bar i');"
            "return r?parseFloat(r.style.width):-1})()" % h)


async def answer_confirm(b, ok, text):
    await until(b, "/%s/.test((document.querySelector('.uiconfirm-msg')||{}).textContent||'')" % text)
    await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"%d\"]').click();true" % (1 if ok else 0))


# ------------------------------------------------------------------------------------------- scenarios

@scenario
async def a_torrent_is_added_downloads_and_removing_it_sticks(S):
    eng = NODE.engines[S.pk]
    eng.add(KEEP, 'Keep Me', progress=0.5)
    b = await S.device()
    await open_downloads(b)
    await until(b, "%s.join()===%s" % (ROWS, json.dumps(KEEP)))

    # Add torrent: the dialog torrents.js puts on the button, a magnet, Add.
    await click(b, '#tm-add')
    await until(b, "!!document.querySelector('#tmx-aq')")
    magnet = 'magnet:?xt=urn:btih:%s&dn=Debian+12' % NEW.upper()
    await b.js("document.querySelector('#tmx-aq').value=%s;document.querySelector('#tmx-aadd').click();true"
               % json.dumps(magnet))
    await until(b, "%s.includes(%s) && /Debian 12/.test(%s)" % (ROWS, json.dumps(NEW), LIST))
    assert ('/add', {'magnet': magnet, 'torrent_url': ''}) in eng.posts(), eng.posts()
    assert '/api/torrent/add' in NODE.posts[S.pk]

    # It downloads: the bar is redrawn by the 2s poll and moves forward, without anyone touching anything.
    first = await b.js(_pct(NEW))
    await until(b, "%s > %s + 10" % (_pct(NEW), first), 20)

    # Another device adds a torrent at the FRONT: every position shifts under the rows on screen. Remove ours.
    eng.add(OTHER, 'Someone Else', front=True)
    await b.js("document.querySelector('#tm-list .tm-item[data-h=\"%s\"] .tm-del').click();true" % NEW)
    await answer_confirm(b, True, 'Remove')
    await answer_confirm(b, False, 'Delete the downloaded files')
    for _ in range(100):
        if any(p == '/remove' for p, _b in eng.posts()):
            break
        await asyncio.sleep(.1)
    removes = [body for p, body in eng.posts() if p == '/remove']
    assert removes == [{'num': 3, 'delete_files': False}], removes
    assert eng.hashes() == [OTHER, KEEP], ('the engine removed the wrong torrent', eng.hashes())
    await until(b, "!%s.includes(%s)" % (ROWS, json.dumps(NEW)))
    await until(b, "%s.join()===%s" % (ROWS, json.dumps(OTHER + ',' + KEEP)))

    # Still gone after a reload.
    await S.open[0].reload()
    await open_downloads(b)
    await until(b, "%s.join()===%s" % (ROWS, json.dumps(OTHER + ',' + KEEP)))
    assert NEW not in await b.js(ROWS)
    assert no_errors(await b.js('__errors')) == []


@scenario
async def the_downloads_screen_says_what_is_wrong_and_recovers(S):
    eng = NODE.engines[S.pk]
    eng.add(KEEP, 'Keep Me', progress=0.2)
    b = await S.device()
    await open_downloads(b)
    await until(b, "%s.join()===%s" % (ROWS, json.dumps(KEEP)))

    # The engine node goes down while the screen is open.
    eng.mode = 'down'
    await until(b, "!document.querySelector('#tm-list .tm-item') && /Couldn.t reach the torrent client/.test(%s)"
                % LIST, 15)
    text = await b.js(LIST)
    assert 'no torrent client enabled' not in text and 'Admin' not in text, text

    # It comes back: the same torrent, no reload, no click.
    eng.mode = 'ok'
    await until(b, "%s.join()===%s" % (ROWS, json.dumps(KEEP)), 12)

    # An engine that never answers, opened fresh: a sentence within seconds, not a spinner for ever.
    eng.mode = 'hang'
    await b.js("__PC.switchView('notifications');true")
    t0 = time.monotonic()
    await open_downloads(b)
    await until(b, "!document.querySelector('#tm-list .spinner') && /Couldn.t reach the torrent client/.test(%s)"
                % LIST, 25)
    took = time.monotonic() - t0
    assert took < 20, ('the Downloads screen spun for', took)

    # A node whose engine really has no torrent client says THAT.
    eng.mode = 'off'
    await click(b, '#tm-refresh')
    await until(b, "/no torrent client enabled/.test(%s)" % LIST, 15)

    # And nothing was ever written on the strength of a failed read.
    assert NODE.posts[S.pk] == [], NODE.posts[S.pk]
    assert eng.posts() == [], eng.posts()
    assert no_errors(await b.js('__errors')) == []


# ------------------------------------------------------------------------------------------- the tests

@CHROME
def test_a_torrent_is_added_downloads_and_removing_it_sticks(ran):
    ran('a_torrent_is_added_downloads_and_removing_it_sticks')


@CHROME
def test_the_downloads_screen_says_what_is_wrong_and_recovers(ran):
    ran('the_downloads_screen_says_what_is_wrong_and_recovers')
