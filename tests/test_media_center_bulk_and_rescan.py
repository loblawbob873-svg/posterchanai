"""Media Center: deleting a SELECTION in one request, and rescanning every library on a timer.

"allow selecting all, selecting none, selecting few, etc so you can better move/delete files" — the
client's multi-select sends its whole selection to `POST /{library}/delete` (Move already took a
list). Run against a real temporary library folder:

  * every selected file is gone from disk, the catalog forgets exactly those titles, and it is saved
    ONCE however many titles went;
  * a title that cannot be deleted is reported by name while the rest still go;
  * only the owning admin can do it, and never while the library scans.

"media center -> rescan library hourly" — `auto_rescan_pass` rescans a library whose last scan is an
interval old, on the node that holds the files only, and a file dropped into the folder shows up
without anybody pressing Rescan.
"""
import asyncio
import os

import pytest

from app.routers import media_center as routes
from app.services import media_center as media
from tests import test_media_center as _shared
from tests.test_media_center import OWNER, VIEWER
from tests.test_media_center_delete import _library, _ids, _committed

api = _shared.api


@pytest.fixture(autouse=True)
def _fresh_state():
    for state in (routes._moving, routes._move_locks, routes._scans, routes._auto_attempted):
        state.clear()
    yield
    for state in (routes._moving, routes._move_locks, routes._scans, routes._auto_attempted):
        state.clear()


def _count_library_writes(monkeypatch):
    writes = []
    real = media.write
    async def counting(key, value):
        if key == "library:abc":
            writes.append(value)
        return await real(key, value)
    monkeypatch.setattr(media, "write", counting)
    return writes


def test_a_selection_is_deleted_in_one_request_and_one_catalog_save(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    film, show = by_path["Inbox/Film.mkv"], by_path["Show.mp4"]
    writes = _count_library_writes(monkeypatch)

    response = client.post("/api/media-center/abc/delete", json={"items": [film["id"], show["id"]]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["deleted"]) == sorted([film["id"], show["id"]]) and body["errors"] == []
    assert not (root / "Inbox" / "Film.mkv").exists() and not (root / "Show.mp4").exists()
    assert not (root / "Inbox" / "Film.en.srt").exists()           # its own subtitles went with it
    assert (root / "Inbox" / "Film.mp4").is_file()                 # another title did not
    assert film["id"] not in _ids(client) and show["id"] not in _ids(client)
    assert film["id"] not in _committed(docs) and show["id"] not in _committed(docs)
    assert docs["library:abc"]["count"] == len(by_path) - 2
    assert len(writes) == 1, f"the catalog was saved {len(writes)} times for one selection"


def test_one_title_that_cannot_go_is_named_and_the_rest_still_go(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    film, show = by_path["Inbox/Film.mkv"], by_path["Show.mp4"]
    real = media.delete_item
    def refuse_the_show(library, item):
        if item["id"] == show["id"]:
            raise media.DeleteRefused("Show.mp4 is a link; it was not deleted")
        return real(library, item)
    monkeypatch.setattr(media, "delete_item", refuse_the_show)

    body = client.post("/api/media-center/abc/delete",
                       json={"items": [film["id"], show["id"], "no-such-id"]}).json()
    assert body["deleted"] == [film["id"]]
    errors = {e["id"]: e for e in body["errors"]}
    assert errors[show["id"]]["name"] == show["name"] and "link" in errors[show["id"]]["error"]
    assert "no-such-id" in errors
    assert not (root / "Inbox" / "Film.mkv").exists() and (root / "Show.mp4").is_file()
    assert show["id"] in _committed(docs) and film["id"] not in _committed(docs)


def test_only_the_owning_admin_and_never_while_scanning(api, monkeypatch):
    client, docs, user, root = api
    film = _library(docs, root, monkeypatch)["Inbox/Film.mkv"]
    url, body = "/api/media-center/abc/delete", {"items": [film["id"]]}
    for npub, admin in ((VIEWER, False), (OWNER, False), (VIEWER, True)):
        user.nostr_npub, user.is_admin = npub, admin
        assert client.post(url, json=body).status_code == 403, (npub, admin)
    user.nostr_npub, user.is_admin = OWNER, True
    routes._scans["abc"] = {"state": "running", "count": 0}
    assert client.post(url, json=body).status_code == 409
    routes._scans.clear()
    assert client.post(url, json={"items": []}).status_code == 400
    assert (root / "Inbox" / "Film.mkv").read_bytes() == b"film-bytes"


def _settings(monkeypatch, **values):
    monkeypatch.setattr(routes.settings_store, "get", lambda key, *a: values.get(key, ""))


def test_the_hourly_pass_finds_a_new_file_without_anyone_pressing_rescan(api, monkeypatch):
    client, docs, user, root = api
    _library(docs, root, monkeypatch)
    docs["library:abc"]["scanned_at"] = 1_000
    (root / "New Film.mkv").write_bytes(b"dropped in later")
    assert not any(i["name"].startswith("New Film") for i in client.get("/api/media-center/abc/items").json()["items"])
    _settings(monkeypatch)                                     # blank = the default, 60 minutes

    scanned = asyncio.run(routes.auto_rescan_pass(now=1_000 + 3600))
    assert scanned == ["abc"]
    names = [i["name"] for i in client.get("/api/media-center/abc/items").json()["items"]]
    assert any(n.startswith("New Film") for n in names), names
    assert routes._scans["abc"]["state"] == "complete"


def test_the_pass_waits_for_the_interval_and_respects_off_and_proxy_nodes(api, monkeypatch):
    client, docs, user, root = api
    _library(docs, root, monkeypatch)
    docs["library:abc"]["scanned_at"] = 10_000
    ran = []
    async def fake_run_scan(library):
        ran.append(library["id"]); routes._scans[library["id"]] = {"state": "complete", "count": 0}
    monkeypatch.setattr(routes, "run_scan", fake_run_scan)

    _settings(monkeypatch)
    assert asyncio.run(routes.auto_rescan_pass(now=10_000 + 3599)) == []      # not an hour yet
    _settings(monkeypatch, media_center_rescan_minutes="0")
    assert asyncio.run(routes.auto_rescan_pass(now=10_000 + 99_999)) == []    # switched off
    _settings(monkeypatch, media_center_server_url="http://nas.lan:3051")
    assert asyncio.run(routes.auto_rescan_pass(now=10_000 + 99_999)) == []    # files live elsewhere
    _settings(monkeypatch, media_center_rescan_minutes="15")
    assert asyncio.run(routes.auto_rescan_pass(now=10_000 + 900)) == ["abc"]
    assert ran == ["abc"]


def test_never_on_top_of_a_scan_or_move_and_a_failing_scan_is_not_retried_every_minute(api, monkeypatch):
    client, docs, user, root = api
    _library(docs, root, monkeypatch)
    docs["library:abc"]["scanned_at"] = 0
    attempts = []
    async def failing_run_scan(library):
        attempts.append(library["id"]); routes._scans[library["id"]] = {"state": "failed", "error": "disk gone"}
    monkeypatch.setattr(routes, "run_scan", failing_run_scan)
    _settings(monkeypatch)

    routes._scans["other"] = {"state": "running", "count": 3}
    assert asyncio.run(routes.auto_rescan_pass(now=50_000)) == []
    routes._scans.clear()
    routes._moving.add("abc")
    assert asyncio.run(routes.auto_rescan_pass(now=50_000)) == []
    routes._moving.clear()

    assert asyncio.run(routes.auto_rescan_pass(now=50_000)) == ["abc"]
    # The scan failed, so scanned_at never moved -- the next minute must NOT try again.
    assert asyncio.run(routes.auto_rescan_pass(now=50_060)) == []
    assert asyncio.run(routes.auto_rescan_pass(now=50_000 + 3600)) == ["abc"]
    assert attempts == ["abc", "abc"]


def test_the_setting_is_declared_and_on_the_admin_page():
    from app.schemas import SettingsResponse
    assert SettingsResponse().media_center_rescan_minutes == 60
    tab = open(os.path.join(os.path.dirname(__file__), "..", "templates", "admin", "tabs", "storage.html")).read()
    assert 'id="media_center_rescan_minutes" name="media_center_rescan_minutes"' in tab
