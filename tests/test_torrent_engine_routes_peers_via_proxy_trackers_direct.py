"""The torrent engine's network routing, read back from a REAL libtorrent session (the owner, 2026-10-23: "make sure
that you didn't break the torrent behavior, data over our http proxy, trackers are direct over udp").

The intended shape, as the engine logs it at start:
  * peer DATA goes through the HTTP proxy (-> Tor): proxy_type http, proxy_peer_connections on;
  * TRACKERS are reached directly, UDP trackers included: proxy_tracker_connections off, force_proxy off (force_proxy
    would also stop the UDP socket that UDP tracker announces use), hostnames resolved locally;
  * peer-to-peer UDP is off (DHT, LSD, uTP) -- it cannot go through an HTTP proxy -- and it is off from the moment
    the session exists, not only once the proxy probe has finished.

Nothing reaches the internet: the "proxy" is a loopback listener that is never sent a byte (no torrent is added).
"""
import socket
from pathlib import Path

import pytest

lt = pytest.importorskip('libtorrent')

from app.services.libtorrent_service import LibtorrentService


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def engine(tmp_path):
    proxy = socket.socket()
    proxy.bind(('127.0.0.1', 0))
    proxy.listen(8)                       # reachable, so the engine takes its "proxy available" path
    svc = LibtorrentService(download_dir=str(tmp_path / 'dl'), proxy_host='127.0.0.1',
                            proxy_port=proxy.getsockname()[1], listen_port=_free_port())
    try:
        yield svc, proxy.getsockname()[1]
    finally:
        try:
            svc.stop()
        finally:
            proxy.close()


def test_peer_data_goes_through_the_http_proxy_and_trackers_go_direct(engine):
    svc, proxy_port = engine
    s = svc.session.get_settings()
    # peer data: through our HTTP proxy
    assert s['proxy_type'] == int(lt.proxy_type_t.http), s['proxy_type']
    assert (s['proxy_hostname'], s['proxy_port']) == ('127.0.0.1', proxy_port)
    assert s['proxy_peer_connections'] is True
    # trackers: direct, UDP trackers included
    assert s['proxy_tracker_connections'] is False, 'tracker announces would be forced through the HTTP proxy'
    assert s['force_proxy'] is False, 'force_proxy also shuts the UDP socket that UDP tracker announces use'
    assert s['proxy_hostnames'] is False
    # peer-to-peer UDP: off (it cannot be proxied)
    for key in ('enable_dht', 'enable_lsd', 'enable_outgoing_utp', 'enable_incoming_utp'):
        assert s[key] is False, key
    assert s['anonymous_mode'] is True


def test_peer_to_peer_udp_is_off_from_the_moment_the_session_exists(tmp_path, monkeypatch):
    """Before the proxy probe finishes (up to ~30 s of retries) the session already exists; it must not be running
    libtorrent's defaults (DHT bootstrapping to public routers, LSD multicast) unproxied meanwhile."""
    born = {}
    real_session = lt.session

    def spy(*args, **kwargs):
        sess = real_session(*args, **kwargs)
        born.update(sess.get_settings())
        return sess

    monkeypatch.setattr('app.services.libtorrent_service.lt.session', spy)
    proxy = socket.socket()
    proxy.bind(('127.0.0.1', 0))
    proxy.listen(8)
    svc = LibtorrentService(download_dir=str(tmp_path / 'dl'), proxy_host='127.0.0.1',
                            proxy_port=proxy.getsockname()[1], listen_port=_free_port())
    try:
        assert born, 'the engine did not create its session through libtorrent.session'
        for key in ('enable_dht', 'enable_lsd', 'enable_outgoing_utp', 'enable_incoming_utp'):
            assert born[key] is False, f'{key} was ON when the session was created'
    finally:
        try:
            svc.stop()
        finally:
            proxy.close()
