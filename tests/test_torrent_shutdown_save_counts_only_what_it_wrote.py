"""The shutdown save says what it did: a late resume answer for a REMOVED torrent is skipped, and it is not counted
or logged as "Saved resume data".

_write_alert_resume deliberately does not write a removed (tombstoned) torrent's late save_resume_data_alert --
that file is what brought removed torrents back on restart (test_torrent_removal_survives_restart_real_engine.py).
But it returned the info-hash either way, so _save_resume_data counted the skipped torrent as saved: a node that
removed a torrent just before a deploy logged "Saved resume data: <removed hash>" and a count one too high --
the log line the next person debugging "the removed torrent reappears" reads first.

The REAL libtorrent engine on a real directory (nothing stubbed but the proxy probe, as the restart test does); the
alert thread is never started, so the shutdown save is the only consumer of the alerts and the run is
deterministic: a save is put in flight for both torrents, one is removed, then the shutdown save runs.
"""
import logging
import socket
import tempfile
from pathlib import Path

import pytest

lt = pytest.importorskip('libtorrent')

from tests.test_torrent_removal_survives_restart_real_engine import _make_torrent  # noqa: E402


def _closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a_removed_torrents_late_resume_answer_is_skipped_and_not_counted_as_saved(monkeypatch, caplog):
    from app.services.libtorrent_service import LibtorrentService
    monkeypatch.setattr(LibtorrentService, '_check_proxy', lambda self, host, port, timeout=5: True)
    work = Path(tempfile.mkdtemp(prefix='bt-save-count-'))
    seeds, downloads = work / 'seeds', work / 'downloads'
    seeds.mkdir()
    gone_data, gone_ih = _make_torrent(seeds, 'Removed Before Deploy.mkv', 120_000)
    keep_data, keep_ih = _make_torrent(seeds, 'Kept.mkv', 90_000)
    svc = LibtorrentService(download_dir=str(downloads), proxy_host='127.0.0.1', proxy_port=_closed_port(),
                            listen_port=0)
    try:
        assert svc.add_torrent_file(gone_data) == gone_ih
        assert svc.add_torrent_file(keep_data) == keep_ih
        for h in list(svc.torrents.values()):               # the periodic save, in flight ...
            h.save_resume_data(lt.torrent_handle.save_info_dict)
        assert svc.remove(gone_ih)                          # ... and the removal lands before its answer
        with caplog.at_level(logging.DEBUG, logger='app.services.libtorrent_service'):
            svc._save_resume_data()
        msgs = [r.getMessage() for r in caplog.records]
        on_disk = {p.stem for p in (downloads / '.resume').glob('*.resume')}
        assert on_disk == {keep_ih}, on_disk                # the skip itself (unchanged behaviour)
        assert not [m for m in msgs if m.startswith('[BT] Saved resume data:') and gone_ih in m], \
            ('a skipped, removed torrent was logged as saved', msgs)
        total = [m for m in msgs if m.startswith('[BT] Saved resume data for ')]
        assert total and total[-1].startswith('[BT] Saved resume data for 1 torrents'), (total, msgs)
        assert [m for m in msgs if 'skipped' in m and gone_ih in m], ('the skip is not said', msgs)
    finally:
        try:
            svc.session.pause()
        except Exception:
            pass
