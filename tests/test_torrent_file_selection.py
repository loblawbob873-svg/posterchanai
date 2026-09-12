"""Per-file selection — download only the files you want out of a torrent.

libtorrent's per-file priority is the mechanism (0 = do not download). The interesting failures are
all quiet ones:

  * a MAGNET has no file list at add time, so a selection made on add has nothing to index into. It
    has to be recorded and applied when the metadata lands — and it has to survive a restart, or the
    torrent silently downloads every file the user deselected;
  * a PARTIAL priority update that rebuilds the whole vector re-enables files somebody switched off;
  * an EMPTY selection is a torrent that can never finish and says nothing about why;
  * a selection left behind after a remove re-applies itself to a later re-add of the same torrent.

The libtorrent handle is faked: a real session needs a proxy, a listen port and a swarm. What is
under test is this repo's logic, and the two libtorrent calls it makes (`prioritize_files`,
`file_priorities`) are asserted by shape.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.routers import torrent as torrent_router
from app.services.libtorrent_service import LibtorrentService


# --------------------------------------------------------------------------- fakes

class _Files:
    def __init__(self, files):
        self._f = files

    def file_path(self, i):
        return self._f[i][0]

    def file_size(self, i):
        return self._f[i][1]


class _Info:
    def __init__(self, files):
        self._files = _Files(files)
        self._n = len(files)

    def num_files(self):
        return self._n

    def files(self):
        return self._files


class _Handle:
    """A libtorrent handle with metadata (files=[(path,size),…]) or without (files=None)."""

    def __init__(self, files=None, progress=None):
        self._info = _Info(files) if files else None
        self.prios = [4] * (len(files) if files else 0)
        self._progress = progress or [0] * (len(files) if files else 0)
        self.valid = True

    def is_valid(self):
        return self.valid

    def torrent_file(self):
        return self._info

    def file_priorities(self):
        return list(self.prios)

    def prioritize_files(self, prios):
        self.prios = list(prios)

    def file_progress(self):
        return list(self._progress)

    def arrive(self, files, progress=None):
        """Metadata lands (the thing that happens seconds after a magnet is added)."""
        self._info = _Info(files)
        self.prios = [4] * len(files)
        self._progress = progress or [0] * len(files)


def _service(tmp_path):
    """A LibtorrentService with only the fields the selection code touches — constructing a real one
    opens a session and demands a reachable proxy."""
    import threading
    s = object.__new__(LibtorrentService)
    s.torrents = {}
    s.resume_dir = tmp_path
    s._select_path = tmp_path / ".select.json"
    s._file_lock = threading.Lock()
    s._pending_select = {}
    return s


PACK = [("Show/S01E01.mkv", 1000), ("Show/S01E02.mkv", 2000),
        ("Show/S01E03.mkv", 3000), ("Show/sample.mkv", 10)]


# --------------------------------------------------------------------------- with metadata

def test_selecting_files_switches_the_others_off_and_downloads_nothing_else(tmp_path):
    s = _service(tmp_path)
    h = _Handle(PACK)
    s.torrents["abc"] = h
    assert s.select_files("abc", [0, 2]) == "applied"
    assert h.prios == [4, 0, 4, 0]


def test_a_file_switched_back_on_resumes_rather_than_being_deleted(tmp_path):
    """Nothing here removes bytes: priority 0 stops fetching the pieces, and 4 starts again."""
    s = _service(tmp_path)
    h = _Handle(PACK, progress=[1000, 500, 0, 0])
    s.torrents["abc"] = h
    s.select_files("abc", [0])
    assert h.prios == [4, 0, 0, 0]
    s.select_files("abc", [0, 1])
    assert h.prios == [4, 4, 0, 0]
    assert s.get_files("abc")[1]["downloaded"] == 500, "progress was thrown away"


def test_a_partial_priority_update_never_re_enables_a_file_that_was_switched_off(tmp_path):
    """The trap: rebuilding the whole priority vector from a PARTIAL map quietly turns every file
    the map does not mention back on — i.e. the one thing the user came here to prevent."""
    s = _service(tmp_path)
    h = _Handle(PACK)
    s.torrents["abc"] = h
    s.select_files("abc", [0])                     # only the first file wanted
    assert h.prios == [4, 0, 0, 0]
    assert s.set_file_priorities("abc", {"2": 7}) is True
    assert h.prios == [4, 0, 7, 0], "a partial update re-enabled files it was not asked about"


def test_get_files_reports_index_priority_and_per_file_progress(tmp_path):
    """Without an index the client cannot NAME a file the way prioritize_files does, and without a
    priority it cannot draw which ones are currently off."""
    s = _service(tmp_path)
    h = _Handle(PACK, progress=[1000, 0, 1500, 0])
    s.torrents["abc"] = h
    s.select_files("abc", [0, 2])
    files = s.get_files("abc")
    assert [f["index"] for f in files] == [0, 1, 2, 3]
    assert [f["wanted"] for f in files] == [True, False, True, False]
    assert [f["priority"] for f in files] == [4, 0, 4, 0]
    assert files[2]["progress"] == 0.5 and files[2]["downloaded"] == 1500
    assert files[0]["path"] == "Show/S01E01.mkv" and files[0]["size"] == 1000


def test_clearing_a_selection_downloads_everything_again(tmp_path):
    s = _service(tmp_path)
    h = _Handle(PACK)
    s.torrents["abc"] = h
    s.select_files("abc", [1])
    s.select_files("abc", None)
    assert h.prios == [4, 4, 4, 4]


# --------------------------------------------------------------------------- before metadata

def test_a_selection_made_on_a_magnet_is_recorded_and_applied_when_the_metadata_arrives(tmp_path):
    """A magnet carries no file list. Applying the selection immediately is impossible, so it is
    deferred — and if it is dropped instead, the torrent downloads everything and nothing says so."""
    s = _service(tmp_path)
    h = _Handle(None)                              # no metadata yet
    s.torrents["abc"] = h
    assert s.select_files("abc", [0, 2]) == "pending"
    assert s.get_files("abc") == []                # nothing to show yet — NOT "no files"
    assert s.pending_selection("abc") == [0, 2]

    s._apply_pending_selections()                  # still no metadata: still pending
    assert s.pending_selection("abc") == [0, 2]

    h.arrive(PACK)
    s._apply_pending_selections()
    assert h.prios == [4, 0, 4, 0]
    assert s.pending_selection("abc") is None, "a pending selection was applied but never cleared"


def test_a_pending_selection_survives_a_restart(tmp_path):
    """The metadata for a torrent with no live peers can take longer than the next deploy. A
    forgotten selection does not fail loudly — it downloads the files the user deselected."""
    s = _service(tmp_path)
    s.torrents["abc"] = _Handle(None)
    s.select_files("abc", [1, 3])
    assert json.loads((tmp_path / ".select.json").read_text()) == {"abc": [1, 3]}

    s2 = _service(tmp_path)                        # "restart"
    s2._pending_select = s2._load_select()
    h = _Handle(PACK)
    s2.torrents["abc"] = h
    s2._apply_pending_selections()
    assert h.prios == [0, 4, 0, 4]


def test_a_selection_does_not_outlive_the_torrent_it_was_made_for(tmp_path):
    """Kept, it re-applies itself to a later re-add of the same torrent — refusing files nobody
    deselected this time."""
    s = _service(tmp_path)
    s.torrents["abc"] = _Handle(None)
    s.select_files("abc", [0])
    del s.torrents["abc"]                          # removed while waiting on metadata
    s._apply_pending_selections()
    assert s.pending_selection("abc") is None


def test_selecting_on_a_torrent_this_node_does_not_have_says_so(tmp_path):
    s = _service(tmp_path)
    assert s.select_files("nope", [0]) == "unknown"
    assert s.set_file_priorities("nope", {"0": 0}) is False


# --------------------------------------------------------------------------- the endpoints

def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _Svc:
    """Just enough of LibtorrentService for the router."""

    def __init__(self, real, hashes):
        self._r = real
        self._h = hashes

    def get_hash_by_number(self, n):
        return self._h.get(n)

    def __getattr__(self, k):
        return getattr(self._r, k)


@pytest.fixture
def _wired(tmp_path, monkeypatch):
    s = _service(tmp_path)
    svc = _Svc(s, {1: "abc"})
    monkeypatch.setattr(torrent_router, "get_bt_service", lambda db: svc)
    monkeypatch.setattr(torrent_router, "get_remote_server_url", lambda db: None)
    return s, svc


def test_the_endpoint_lists_files_and_says_whether_the_metadata_has_arrived(_wired):
    s, _svc = _wired
    h = _Handle(None)
    s.torrents["abc"] = h
    r = _run(torrent_router.list_torrent_files(1, None, None, None))
    assert r["files"] == [] and r["metadata"] is False, \
        "an empty list with no explanation is indistinguishable from a torrent with no files"
    h.arrive(PACK)
    r = _run(torrent_router.list_torrent_files(1, None, None, None))
    assert r["metadata"] is True and len(r["files"]) == 4


def test_the_endpoint_saves_a_selection(_wired):
    s, _svc = _wired
    h = _Handle(PACK)
    s.torrents["abc"] = h
    body = torrent_router.TorrentFilesRequest(num=1, files=[1, 2])
    r = _run(torrent_router.set_torrent_files(body, None, None, None))
    assert h.prios == [0, 4, 4, 0] and r["selection"] == "applied"


def test_an_empty_selection_is_refused_rather_than_pausing_by_the_back_door(_wired):
    """Every file at priority 0 is a download that can never finish and never explains itself."""
    from fastapi import HTTPException
    s, _svc = _wired
    h = _Handle(PACK)
    s.torrents["abc"] = h
    with pytest.raises(HTTPException) as e:
        _run(torrent_router.set_torrent_files(
            torrent_router.TorrentFilesRequest(num=1, files=[]), None, None, None))
    assert e.value.status_code == 400 and "Pause" in e.value.detail
    assert h.prios == [4, 4, 4, 4], "the torrent was switched off anyway"


def test_add_carries_a_file_selection(_wired):
    """The API half of "choose what to download": a caller that already knows the indices gets them
    applied before the first piece, rather than after the client notices."""
    s, svc = _wired
    h = _Handle(PACK)

    def _add_magnet(magnet, user_id=None):
        s.torrents["abc"] = h
        return "abc"

    svc.add_magnet = _add_magnet
    body = torrent_router.AddTorrentRequest(magnet="magnet:?xt=urn:btih:" + "a" * 40, files=[3])
    r = _run(torrent_router.add_torrent(body, None, None, None))
    assert r["selection"] == "applied"
    assert h.prios == [0, 0, 0, 4]


def test_add_without_a_selection_still_downloads_everything(_wired):
    """Every existing caller sends no `files`, and must keep behaving exactly as it did."""
    s, svc = _wired
    h = _Handle(PACK)
    svc.add_magnet = lambda magnet, user_id=None: (s.torrents.__setitem__("abc", h), "abc")[1]
    body = torrent_router.AddTorrentRequest(magnet="magnet:?xt=urn:btih:" + "a" * 40)
    r = _run(torrent_router.add_torrent(body, None, None, None))
    assert r["selection"] == "" and h.prios == [4, 4, 4, 4]


def test_the_client_sends_the_fields_the_endpoints_declare():
    """Same contract check the existing torrent API test makes, for the two new endpoints: the
    action endpoints address a torrent by POSITION, and a field the model does not declare 422s."""
    import re
    root = Path(__file__).resolve().parent.parent
    js = (root / "static" / "js" / "client" / "torrents.js").read_text()
    api = (root / "app" / "routers" / "torrent.py").read_text()

    assert "class TorrentFilesRequest" in api
    bodies = re.findall(r"api\('/files'\s*,\s*\{[^}]*?body:JSON\.stringify\(\{([^}]*)\}\)", js)
    assert bodies, "the client no longer POSTs /files"
    for b in bodies:
        keys = set(re.findall(r"(\w+)\s*[:,}]", b)) & {"num", "files", "priorities", "info_hash"}
        assert "info_hash" not in b, "/files is addressed by position, not hash — this would 422"
        assert "num" in keys
    assert "numFor" in js, "the hash→num resolver is gone; a stale position names another torrent"
