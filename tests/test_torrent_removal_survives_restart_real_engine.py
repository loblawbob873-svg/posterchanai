"""A torrent removed from the Downloads list stays removed across an engine restart -- the REAL libtorrent engine,
behind the REAL router, reached the way a member's node reaches it.

The bug class ("the removed torrent reappears", docs: the 'Orange Is the New Black' bug) lives in what is on disk
when the engine starts again: one `.resume` file left behind for a removed torrent and the next start re-adds it.
tests/test_torrent_resume_purge.py states the purge rule against a stubbed libtorrent; this drives the whole chain
with nothing stubbed but the network:

    server1's app/routers/torrent.py  --(forward_to_remote, peer-authenticated)-->  the engine node's
    app/routers/torrent.py  -->  a real LibtorrentService (libtorrent 2.x) on a real download dir

Two torrents are added (real .torrent files built here -- hybrid v1+v2, NO trackers -- fetched by the engine node
from a loopback URL, exactly the `torrent_url` path), the periodic resume save has written both, and then a save
request is put IN FLIGHT and the first torrent is removed through server1 in the same instant (the late
save_resume_data_alert that once rewrote a removed torrent's file). The engine is stopped the way a deploy stops
it and a fresh one is started on the same directory: the kept torrent must come back -- so "nothing came back"
cannot pass vacuously -- and the removed one must not.

Nothing goes to the internet: no trackers in either torrent, DHT/LSD/uTP are off in the shipped settings, the proxy
is a closed loopback port, and the listen socket is ephemeral. (UPnP/NAT-PMP are still libtorrent's default ON in the
shipped settings, so the engine may ask the LAN gateway for a port mapping -- a LAN-only exchange.)
"""
import asyncio
import socket
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

lt = pytest.importorskip('libtorrent')

SECRET = 'peer-secret-for-this-test'


def _closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_torrent(src_dir: Path, name: str, size: int) -> tuple[bytes, str]:
    (src_dir / name).write_bytes((name.encode() * (size // len(name) + 1))[:size])
    fs = lt.file_storage()
    lt.add_files(fs, str(src_dir / name))
    ct = lt.create_torrent(fs, 16 * 1024)          # libtorrent 2.x: a HYBRID (v1 + v2) torrent by default
    lt.set_piece_hashes(ct, str(src_dir))
    data = lt.bencode(ct.generate())
    ti = lt.torrent_info(lt.bdecode(data))
    return data, str(ti.info_hashes().v1)


class Uvicorn:
    """A real HTTP listener for an ASGI app -- forward_to_remote dials a URL, so the engine node must be one."""

    def __init__(self, app):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning',
                                                    lifespan='off'))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(.05)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


def test_a_removed_torrent_does_not_come_back_after_the_engine_restarts(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.database import get_db
    from app.routers import torrent
    from app.services import settings_store
    from app.services.libtorrent_service import LibtorrentService

    work = Path(tempfile.mkdtemp(prefix='bt-restart-'))
    seeds, downloads = work / 'seeds', work / 'downloads'
    seeds.mkdir()
    gone_data, gone_ih = _make_torrent(seeds, 'Orange Is the New Black S01.mkv', 300_000)
    keep_data, keep_ih = _make_torrent(seeds, 'Keep This One.mkv', 200_000)

    # The .torrent files, served from loopback (the engine node downloads `torrent_url` itself).
    (seeds / 'gone.torrent').write_bytes(gone_data)
    (seeds / 'keep.torrent').write_bytes(keep_data)
    files = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(seeds)))
    files.daemon_threads = True
    threading.Thread(target=files.serve_forever, daemon=True).start()
    src = 'http://127.0.0.1:%d/' % files.server_address[1]

    # The proxy is configured (the engine refuses to run without one) and is a closed port: with no trackers and
    # DHT/LSD/uTP off there is nothing for it to carry. Skipping the reachability probe only skips its retries.
    monkeypatch.setattr(LibtorrentService, '_check_proxy', lambda self, host, port, timeout=5: True)
    dead = _closed_port()

    from app.services import libtorrent_service
    births, real_session = [], lt.session

    def session(*a, **k):
        sess = real_session(*a, **k)
        got = sess.get_settings()
        births.append({'enable_dht': got['enable_dht'], 'enable_lsd': got['enable_lsd']})
        return sess
    monkeypatch.setattr(libtorrent_service.lt, 'session', session)

    def engine():
        return LibtorrentService(download_dir=str(downloads), proxy_host='127.0.0.1', proxy_port=dead,
                                 listen_port=0)
    current = {'svc': engine()}
    current['svc'].start()

    # Which node a request is on is the db marker each app hands its routes; everything else is the shipped router.
    real_get = settings_store.get
    engine_url = {}

    def get(key, *a, **k):
        if key == 'bt_server_url':
            return engine_url['url']
        if key == 'lb_shared_secret':
            return SECRET
        return real_get(key, *a, **k)
    monkeypatch.setattr(settings_store, 'get', get)
    monkeypatch.setattr(torrent, 'get_remote_server_url', lambda db: engine_url['url'] if db == 'server1' else None)
    monkeypatch.setattr(torrent, 'get_bt_service', lambda db: current['svc'] if db == 'engine' else None)

    node = FastAPI()
    node.include_router(torrent.router)
    node.dependency_overrides[get_db] = lambda: 'engine'
    member = SimpleNamespace(id=7, is_admin=False, can_torrent=True)
    server1 = FastAPI()
    server1.include_router(torrent.router)
    server1.dependency_overrides[get_db] = lambda: 'server1'
    server1.dependency_overrides[torrent.get_torrent_user] = lambda: member

    async def flow():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server1), base_url='http://server1',
                                     timeout=60) as c:
            async def listing():
                r = await c.get('/api/torrent/list')
                assert r.status_code == 200, r.text
                return {t['info_hash']: t['num'] for t in r.json()['torrents']}

            for name in ('gone.torrent', 'keep.torrent'):
                r = await c.post('/api/torrent/add', json={'torrent_url': src + name})
                assert r.status_code == 200, r.text
            assert set(await listing()) == {gone_ih, keep_ih}

            # The periodic resume save has run: both are on disk, as they would be on any node up > 30s.
            svc = current['svc']
            svc._save_resume_data()
            on_disk = {p.stem for p in (downloads / '.resume').glob('*.resume')}
            assert {gone_ih, keep_ih} <= on_disk, on_disk

            # A save request in flight (the periodic loop's), and the removal lands before its answer does.
            for h in list(svc.torrents.values()):
                h.save_resume_data(lt.torrent_handle.save_info_dict)
            r = await c.post('/api/torrent/remove', json={'num': (await listing())[gone_ih], 'delete_files': False})
            assert r.status_code == 200, r.text
            assert set(await listing()) == {keep_ih}
            await asyncio.sleep(3)                    # the alert loop (2s tick) sees the late save answers

            # A deploy: the engine stops (and saves what it holds), a fresh one starts on the same directory.
            svc.stop()
            del svc
            current['svc'] = engine()
            current['svc'].start()
            after = await listing()
            assert keep_ih in after, ('the kept torrent did not come back -- the restart proves nothing', after)
            assert gone_ih not in after, 'the REMOVED torrent came back after the engine restarted'
            left = {p.stem for p in (downloads / '.resume').glob('*.resume')}
            assert left == {keep_ih}, ('resume files left behind', left)

    try:
        with Uvicorn(node) as up:
            engine_url['url'] = 'http://127.0.0.1:%d' % up.port
            asyncio.run(flow())
    finally:
        try:
            current['svc'].stop()
        except Exception:
            pass
        files.shutdown()

    # Every session the engine made was BORN with DHT and Local Service Discovery off -- not switched off a
    # moment later by apply_settings, after the proxy probe, having already started unproxied.
    assert len(births) == 2 and all(b == {'enable_dht': False, 'enable_lsd': False} for b in births), births
