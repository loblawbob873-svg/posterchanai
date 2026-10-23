"""Folder Sync, end to end: a real folder on one device arrives -- byte for byte -- in a real folder on a FRESH
device of the same account, and an edit and a deletion made there come back. Driven through the screen.

What is real here, and what stands in:

  the client     the SHIPPED web client in real Chrome (tests/client/wallpaper_harness.py: a real relay, the
                 drive index and Blossom); every step is a button a person presses on Sidebar -> Folder Sync --
                 "Add a folder...", the name prompt, "Start syncing", "Set up on this device...", "Sync now".
  the disk       the SHIPPED desktop/fsbridge.js against a real temporary directory per device, run under node
                 (tests/client/fsbridge_rpc.cjs). In the desktop app the page reaches it through preload.js ->
                 IPC -> main.js; here the page's `window.pcFs` forwards each call over loopback HTTP instead,
                 with exactly preload's method list (read from desktop/preload.js, never typed here). The folder
                 picker "chooses" the device's directory; power is a plugged-in desktop.
  the server     the REAL `POST /client/sync-state` (app/routers/client.py: device tokens, per-file CAS, era,
                 tombstones), mounted in this process, its `nostr_store` reads and writes going to the harness's
                 relay. Only the account lookup and the storage key (one per account, derived here) stand in.

  replicate      device A adds a folder holding a nested file and a binary one and starts it; device B -- a
                 fresh Chrome profile, its own empty directory -- is offered the folder under "Synced on your
                 other devices", sets it up and starts it, and ends with the same bytes at the same paths.
  edit + delete  B changes one file and deletes another, presses Sync now; A presses Sync now and has the new
                 bytes, and the deleted file is gone from A's disk -- and only that one.
  no server      the server never answers B's folder reads (503): B's card says so in words, B's directory is
                 exactly what it was, nothing is written to the relay and A's folder is untouched; once the
                 server answers again, Sync now completes the join.
  slow server    every answer to B takes seconds (the reads and the upload of B's own file): the join still
                 completes, both ways.
  writes refused the server answers reads and refuses every write: a file saved meanwhile stays, nothing is
                 recorded, the card says the sync failed -- and when the network is back (the app's `online`
                 signal) the file goes up on its own, without "nothing changed since the last sweep".

Two bugs this found, each fixed with it:
  * an edit a person made on a desktop to a file that had arrived from another device was NEVER published: the
    executor's Android "rewritten after downloading" guard applied on every platform (syncexec.js; now only where
    the adapter says `rewritesDownloads`, i.e. fs-android.js). The card even went back to "in step".
  * a sweep that THREW (the server refused the upload) left the folder clean, so every automatic retry for the
    next 15 minutes was declined as "nothing changed since the last sweep" (sync.js `sweep`'s catch).

Fail-first: PC_WALLPAPER_OVERRIDE="/static/js/client/sync.js=/tmp/broken/sync.js,..." serves a copy of a shipped
client file in its place (see test_private_docs_save_with_a_silent_relay_full_app.py).
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.client import wallpaper_harness as H
from tests.client.test_private_docs_save_with_a_silent_relay_full_app import no_errors, run_scenarios

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = Path(__file__).with_name('fsbridge_rpc.cjs')
NODE_BIN = shutil.which('node')
CHROME = pytest.mark.skipif(not Path(H.CHROME).exists() or not NODE_BIN, reason='Chrome and node required')
FEATURE_COVERS = ('sync',)
SCENARIOS = {}
FIRST_FILL = 170           # account keys 170, 171, ... -- no other suite's
COST = {'an_edit_and_a_deletion_on_one_device_reach_the_other': 40,
        'a_folder_reaches_a_fresh_device_byte_for_byte': 30,
        'a_server_that_does_not_answer_writes_nothing_and_says_so': 40,
        'a_slow_server_still_completes_the_join': 35,
        'a_server_that_refuses_a_write_keeps_the_file_and_says_so': 25,
        'a_server_that_keeps_refusing_is_retried_on_a_backoff_not_on_every_focus': 90}
PAIR = 'Documents'


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def _pubkey(fill):
    import coincurve
    return coincurve.PrivateKey(bytes([fill]) * 32).public_key.format(True)[1:].hex()


def _preload_methods():
    """The page-facing pcFs methods, as desktop/preload.js exposes them."""
    src = (ROOT / 'desktop/preload.js').read_text()
    block = src[src.index("exposeInMainWorld('pcFs', {"):]
    block = block[:block.index('\n  });')]
    return [m for m in re.findall(r'^    (\w+): ', block, re.M) if m != 'onChanged']


# ------------------------------------------------------------------------------------------------ the server

class Node:
    """The node's REAL /client/sync-state, in this process, against the harness relay. `down` (pubkeys): the route
    answers that account 503 without asking the router; `slow` (pubkey -> seconds): each answer waits first."""

    def __init__(self, pubkeys):
        self.users = {pk: SimpleNamespace(id=2000 + i, username='fs%d' % i, nostr_npub=pk,
                                          email='fs%d@example.invalid' % i) for i, pk in enumerate(pubkeys)}
        self.down, self.slow, self.no_writes = set(), {}, set()
        self.calls = []            # (pubkey, status)
        self.loop = self.client = None

    @staticmethod
    def storage_sk(user):
        return hashlib.sha256(b'pc-test-fs-storage:' + str(user.nostr_npub).encode()).digest()

    def storage_pk(self, pk):
        from app.services.nostr import bip340
        return bip340.pubkey_from_seckey(self.storage_sk(self.users[pk])).hex()

    def start(self, mp):
        import httpx
        from fastapi import FastAPI
        from app.database import get_db
        from app.routers import client as client_router
        from app.services import nostr_store
        from app.services.nostr import nostr_service
        from tests.member_fixtures import allow_keys

        by_npub = {nostr_service.npub_of(pk): u for pk, u in self.users.items()}

        async def aby_npub(_db, npub):
            return by_npub.get(npub)
        mp.setattr(client_router.user_table, 'aby_npub', aby_npub)
        mp.setattr(nostr_store, 'user_storage_seckey', lambda _db, u: self.storage_sk(u), raising=False)
        real_setting = client_router._setting
        mp.setattr(client_router, '_setting', lambda db, key, default='': (
            str(H.RELAY.port) if key == 'nostr_relay_port' else real_setting(db, key, default)))
        allow_keys(mp, *self.users)
        app = FastAPI()
        app.include_router(client_router.router)
        app.dependency_overrides[get_db] = lambda: None
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://node')
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
        if path.split('?')[0] == '/api/auth/nostr-login' and method == 'POST':
            # The app session the screen's instance gate asks for (a bearer per account) -- a stand-in.
            pk = (json.loads(body or b'{}') or {}).get('pubkey') or ''
            if pk not in self.users:
                return False
            u = self.users[pk]
            h._json({'access_token': 'tok-' + pk, 'token_type': 'bearer',
                     'user': {'id': u.id, 'username': u.username, 'nostr_npub': pk}})
            return True
        if path.split('?')[0] == '/api/instance-welcome/access' and method == 'GET':
            pk = (h.headers.get('Authorization') or '').removeprefix('Bearer ').strip()[4:]
            if pk not in self.users:
                return False
            h._json({'pubkey': pk, 'qualified': True})
            return True
        if path.split('?')[0] != '/client/sync-state' or method != 'POST':
            return False
        try:
            pk = (json.loads(body or b'{}') or {}).get('pubkey') or ''
        except ValueError:
            pk = ''
        if pk in self.slow:
            time.sleep(self.slow[pk])
        try:
            writes = bool((json.loads(body or b'{}') or {}).get('put'))
        except ValueError:
            writes = False
        if pk in self.down or (writes and pk in self.no_writes):
            self.calls.append((pk, 503))
            h._json({'ok': False, 'error': 'sync state unavailable'}, 503)
            return True
        r = asyncio.run_coroutine_threadsafe(
            self.client.post(path, content=body, headers={'content-type': 'application/json'}), self.loop).result(90)
        self.calls.append((pk, r.status_code))
        h.send_response(r.status_code)
        h.send_header('Content-Type', 'application/json')
        h.send_header('Content-Length', str(len(r.content)))
        h.end_headers()
        h.wfile.write(r.content)
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


# ------------------------------------------------------------------------------------------------ a device

SHIM = r'''(()=>{const B='http://127.0.0.1:__PORT__';
  const b64e=u=>{let s='';for(let i=0;i<u.length;i+=8192)s+=String.fromCharCode.apply(null,u.subarray(i,i+8192));return btoa(s)};
  const enc=v=>(v instanceof Uint8Array)?{$b64:b64e(v)}:(v instanceof ArrayBuffer?{$b64:b64e(new Uint8Array(v))}:v);
  const dec=v=>(v&&typeof v==='object'&&typeof v.$b64==='string')?Uint8Array.from(atob(v.$b64),c=>c.charCodeAt(0)):v;
  const call=async(m,a)=>{const r=await fetch(B+'/call',{method:'POST',body:JSON.stringify({m,a:a.map(enc)})});
    const j=await r.json();if(!j.ok)throw new Error(j.error);return dec(j.r)};
  const fs={};for(const m of __METHODS__)fs[m]=(...a)=>call(m,a);
  fs.onChanged=fn=>{if(typeof fn!=='function')return;(async()=>{for(;;){try{
      const ids=await(await fetch(B+'/changed')).json();for(const id of ids){try{fn(id)}catch(_){}}}
    catch(_){await new Promise(r=>setTimeout(r,1000))}}})()};
  window.pcFs=fs;})();'''


class Disk:
    """One device's folder on disk + the shipped bridge serving it."""

    def __init__(self, files=None):
        self.tmp = tempfile.mkdtemp(prefix='pc-fsync-')
        self.dir = os.path.join(self.tmp, PAIR)
        os.makedirs(self.dir)
        for rel, data in (files or {}).items():
            self.write(rel, data)
        self.proc = subprocess.Popen([NODE_BIN, str(BRIDGE), self.dir], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        self.port = int(self.proc.stdout.readline().strip())

    def write(self, rel, data):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        Path(p).write_bytes(data)

    def snapshot(self):
        """{relative path: bytes} of every file in the folder (the bridge's own bookkeeping dirs excluded)."""
        out = {}
        for d, dirs, files in os.walk(self.dir):
            dirs[:] = [x for x in dirs if x not in ('.pc-trash',)]
            for f in files:
                if f.endswith('.pcpart'):        # a download in flight (the bridge's atomic write), not a file yet
                    continue
                p = os.path.join(d, f)
                try:
                    out[os.path.relpath(p, self.dir).replace(os.sep, '/')] = Path(p).read_bytes()
                except FileNotFoundError:        # renamed into place between the listing and the read
                    continue
        return out

    def close(self):
        try:
            self.proc.kill()
            self.proc.wait(5)
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


async def device(S, disk):
    """A device of this scenario's account with `disk` as its filesystem: signed in, then reloaded with the bridge
    present from the first line of the page, as the desktop app's preload is."""
    b = await S.device()
    shim = SHIM.replace('__PORT__', str(disk.port)).replace('__METHODS__', json.dumps(_preload_methods()))
    await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': shim})
    await S.open[-1].reload()
    await H.wait(b, '!!window.pcFs && !!window.PCSync')
    return b


async def open_sync(b):
    await b.js("__PC.switchView('sync');true")
    await H.wait(b, "!!document.getElementById('sync-add')", 40,
                 state="({view:__PC.VIEW,feed:(document.querySelector('#feed')||{}).innerHTML.slice(0,400),errors:__errors,"
                       "toasts:__toasts,PCSync:!!window.PCSync})")


async def click(b, sel, text=None):
    """Press a control on the screen, as a person does (waits for it to be drawn first)."""
    pick = ("[...document.querySelectorAll(%s)].find(x=>!%s||x.textContent.includes(%s))"
            % (json.dumps(sel), json.dumps(text or ''), json.dumps(text or '')))
    await H.wait(b, '!!(%s)' % pick, 40, state="({toasts:__toasts,errors:__errors,feed:(document.querySelector('#feed')||{}).innerText})")
    await b.js('(%s).click();true' % pick)


def card_status(b):
    return b.js("(()=>{const s=document.querySelector('.sync-card .sync-status');return s?s.textContent:''})()")


async def settled(b, seconds=90):
    """No sweep running or queued on this page, and the card says how the last one ended."""
    await H.wait(b, "!!window.PCSync && !PCSync.busyNow() && (()=>{const s=document.querySelector('.sync-card .sync-status');"
                    "return !!s && !/^(not synced yet|syncing|checking|starting)/.test(s.textContent.trim())})()", seconds,
                 state="({status:(document.querySelector('.sync-card .sync-status')||{}).textContent,toasts:__toasts,errors:__errors})")


async def add_folder(b):
    """Folder Sync -> Add a folder... -> the picker -> name it -> Start syncing."""
    await open_sync(b)
    await click(b, '#sync-add')
    await H.answer_prompt(b, PAIR)
    await click(b, '.sync-card .sync-start')
    await settled(b)


async def join_folder(b):
    """A device with no folder: the account's folder is offered; Set up on this device... -> Start syncing."""
    await open_sync(b)
    await click(b, '.sync-attach[data-key="%s"]' % PAIR)
    await click(b, '.sync-card .sync-start')


def records(S):
    """This account's folder records on the relay: {d: envelope}."""
    spk = NODE.storage_pk(S.pk)
    out = {}
    for e in H.STATE['events']:
        if e.get('pubkey') == spk and e.get('kind') == 30078 and H._d(e).startswith('pcai:fs:%s:' % PAIR):
            try:
                out[H._d(e)] = json.loads(e['content'])
            except ValueError:
                out[H._d(e)] = {'unreadable': True}
    return out


def live(S):
    return [d for d, env in records(S).items() if not env.get('t')]


async def until(fn, seconds, what):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        got = fn()
        if got:
            return got
        await asyncio.sleep(.25)
    raise AssertionError({'waiting for': what, 'got': fn()})


FILES = {'notes.txt': b'hello from device A\n',
         'deep/inside/photo.bin': bytes(range(256)) * 1200,        # 300 KB, every byte value
         'empty.txt': b''}


# ------------------------------------------------------------------------------------------------ scenarios

@scenario
async def a_folder_reaches_a_fresh_device_byte_for_byte(S):
    da, db = Disk(FILES), Disk()
    try:
        a = await device(S, da)
        await add_folder(a)
        assert len(live(S)) == len(FILES), ('device A did not publish one record per file', records(S),
                                            await card_status(a))
        b = await device(S, db)
        await join_folder(b)
        await until(lambda: db.snapshot() == FILES, 90, ('device B holds exactly the folder', list(db.snapshot())))
        await settled(b)
        assert da.snapshot() == FILES, 'joining changed the first device\'s folder'
        assert not no_errors(await a.js('__errors')) and not no_errors(await b.js('__errors'))
    finally:
        da.close(); db.close()


def test_a_folder_reaches_a_fresh_device_byte_for_byte(ran):
    ran('a_folder_reaches_a_fresh_device_byte_for_byte')


@scenario
async def an_edit_and_a_deletion_on_one_device_reach_the_other(S):
    da, db = Disk(FILES), Disk()
    try:
        a = await device(S, da)
        await add_folder(a)
        b = await device(S, db)
        await join_folder(b)
        await until(lambda: db.snapshot() == FILES, 90, 'device B holds the folder')
        await settled(b)
        # On B: change one file, delete another.
        db.write('notes.txt', b'edited on device B\n')
        os.remove(os.path.join(db.dir, 'deep/inside/photo.bin'))
        await click(b, '.sync-card .sync-now')
        await until(lambda: any(e.get('t') for e in records(S).values()), 60, 'B publishes the deletion')
        await settled(b)
        await click(a, '.sync-card .sync-now')
        want = {'notes.txt': b'edited on device B\n', 'empty.txt': b''}
        await until(lambda: da.snapshot() == want, 90, ('device A carries out the edit and the deletion',
                                                       {k: len(v) for k, v in da.snapshot().items()}))
        await settled(a)
        assert db.snapshot() == want
    finally:
        da.close(); db.close()


def test_an_edit_and_a_deletion_on_one_device_reach_the_other(ran):
    ran('an_edit_and_a_deletion_on_one_device_reach_the_other')


@scenario
async def a_server_that_does_not_answer_writes_nothing_and_says_so(S):
    mine = {'local-only.txt': b'B had this before it joined\n'}
    da, db = Disk(FILES), Disk(mine)
    try:
        a = await device(S, da)
        await add_folder(a)
        before = json.dumps(records(S), sort_keys=True)
        b = await device(S, db)
        await open_sync(b)
        await H.wait(b, "!!document.querySelector('.sync-attach[data-key=\"%s\"]')" % PAIR)
        NODE.down.add(S.pk)                     # from here on, nobody answers this account's folder reads
        await join_folder(b)
        await settled(b, 120)
        status = await card_status(b)
        # The card names the failure and says what it did about it -- not "in step", not a spinner.
        assert re.search(r'could not read', status, re.I) and re.search(r'nothing has been changed', status, re.I), (
            'no explicit failure on the card', status)
        assert db.snapshot() == mine, 'a device that could not ask the server changed its folder'
        assert json.dumps(records(S), sort_keys=True) == before, 'records changed while the server was down'
        assert da.snapshot() == FILES
        # The server answers again: the next press completes the join, the device's own file joins the folder.
        NODE.down.discard(S.pk)
        await click(b, '.sync-card .sync-now')
        await until(lambda: db.snapshot() == dict(FILES, **mine), 90, ('the join completes once the server answers',
                                                                      list(db.snapshot())))
        await settled(b)
    finally:
        NODE.down.discard(S.pk)
        da.close(); db.close()


def test_a_server_that_does_not_answer_writes_nothing_and_says_so(ran):
    ran('a_server_that_does_not_answer_writes_nothing_and_says_so')


@scenario
async def a_slow_server_still_completes_the_join(S):
    """Every answer to B takes seconds (reads, the upload of B's own file, the token): the join completes, both ways."""
    mine = {'from-b.txt': b'B brings this one\n'}
    da, db = Disk(FILES), Disk(mine)
    try:
        a = await device(S, da)
        await add_folder(a)
        b = await device(S, db)
        await open_sync(b)
        await H.wait(b, "!!document.querySelector('.sync-attach[data-key=\"%s\"]')" % PAIR)
        NODE.slow[S.pk] = 3.0
        await join_folder(b)
        both = dict(FILES, **mine)
        await until(lambda: db.snapshot() == both, 120, ('B holds the folder despite a slow server', list(db.snapshot())))
        await until(lambda: len(live(S)) == len(both), 120, 'B\'s own file is recorded despite a slow server')
        await settled(b, 120)
    finally:
        NODE.slow.pop(S.pk, None)
        da.close(); db.close()


def test_a_slow_server_still_completes_the_join(ran):
    ran('a_slow_server_still_completes_the_join')


@scenario
async def a_server_that_refuses_a_write_keeps_the_file_and_says_so(S):
    """A file saved while the server refuses every write: the automatic sync (the folder watcher) fails and the
    card says so, the file stays, nothing is recorded -- and when the network comes back (`online`, the app's own
    signal) the file goes up without anybody pressing anything."""
    da = Disk(FILES)
    new = {'new.txt': b'written while the server refuses writes\n'}
    try:
        a = await device(S, da)
        await add_folder(a)
        before = json.dumps(records(S), sort_keys=True)
        NODE.no_writes.add(S.pk)                 # reads are answered, every write is refused (503)
        refused = len([c for c in NODE.calls if c == (S.pk, 503)])
        da.write('new.txt', new['new.txt'])
        await until(lambda: len([c for c in NODE.calls if c == (S.pk, 503)]) > refused, 60,
                    'the watcher\'s sweep tries to publish the new file')
        failed = ("(()=>{const s=document.querySelector('.sync-card .sync-status');"
                  "return !!s && /unavailable|fail|could not|couldn/i.test(s.textContent)})()")
        await H.wait(a, "!PCSync.busyNow() && " + failed, 40,
                     state="({status:(document.querySelector('.sync-card .sync-status')||{}).textContent})")
        assert json.dumps(records(S), sort_keys=True) == before, 'a record changed though every write was refused'
        assert da.snapshot() == dict(FILES, **new)
        # The server takes writes again and the network says it is back: the file goes up on its own.
        NODE.no_writes.discard(S.pk)
        await a.js("window.dispatchEvent(new Event('online'));true")
        await until(lambda: len(live(S)) == len(FILES) + 1, 30,
                    ('the file is published once the network is back', len(live(S))))
        await settled(a)
        assert not re.search(r'nothing changed', await card_status(a)), await card_status(a)
    finally:
        NODE.no_writes.discard(S.pk)
        da.close()


def test_a_server_that_refuses_a_write_keeps_the_file_and_says_so(ran):
    ran('a_server_that_refuses_a_write_keeps_the_file_and_says_so')


@scenario
async def a_server_that_keeps_refusing_is_retried_on_a_backoff_not_on_every_focus(S):
    """A PERMANENT failure (the server refuses every write, for good) while the window keeps getting focus -- a
    desktop being alt-tabbed every 2 s for a minute: the failed folder is retried soon after the failure, then at
    growing intervals -- never one full sweep per focus. Counted at the server: one refused write = one sweep."""
    da = Disk(FILES)
    try:
        a = await device(S, da)
        await add_folder(a)
        NODE.no_writes.add(S.pk)
        n0 = len([c for c in NODE.calls if c == (S.pk, 503)])
        da.write('new.txt', b'written while the server refuses writes for good\n')
        await until(lambda: len([c for c in NODE.calls if c == (S.pk, 503)]) > n0, 60, 'the first refused sweep')
        n1 = len([c for c in NODE.calls if c == (S.pk, 503)])
        t0 = time.monotonic()
        while time.monotonic() - t0 < 60:
            await a.js("window.dispatchEvent(new Event('focus'));true")
            await asyncio.sleep(2)
        sweeps = len([c for c in NODE.calls if c == (S.pk, 503)]) - n1
        print('PERMANENT-FAILURE sweeps in 60 s of focus every 2 s: %d' % sweeps)
        assert sweeps <= 3, ('a permanently failing folder re-swept on focus', sweeps)
        assert sweeps >= 1, 'the failed upload was never retried within a minute'
        assert da.snapshot()['new.txt'].startswith(b'written while')
    finally:
        NODE.no_writes.discard(S.pk)
        da.close()


def test_a_server_that_keeps_refusing_is_retried_on_a_backoff_not_on_every_focus(ran):
    ran('a_server_that_keeps_refusing_is_retried_on_a_backoff_not_on_every_focus')


pytestmark = CHROME
