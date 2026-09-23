"""Media Center → Move: an admin organises a library, and the FILES move on disk.

Reported as "Admins should be able to move videos into different folders to organize. Make sure it
moves on disk too." These run the shipped endpoint against a real temporary library folder:

  * the file (and its subtitles and poster) is renamed on disk, into a new folder when asked;
  * the catalog entry keeps its ID, so watch progress and Jellyfin links survive, and the next
    rescan REUSES the entry instead of re-probing the file under a new id;
  * nothing is ever overwritten, and a move cannot leave the library (`..`, a symlink, an ignored
    folder) or run while the library is scanning;
  * only the owning admin can do it.
"""
import asyncio
import os

import pytest

from app.routers import media_center as routes
from app.services import media_center as media
from tests import test_media_center as _shared
from tests.test_media_center import OWNER, VIEWER

api = _shared.api          # the same fixture: a real library folder, fake relay documents


def _library(docs, root, monkeypatch):
    """A real folder, scanned by the real scanner (probe stubbed), committed as the catalog."""
    (root / "Inbox").mkdir()
    (root / "Inbox" / "Film.mkv").write_bytes(b"film-bytes")
    (root / "Inbox" / "Film.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    (root / "Inbox" / "Film.jpg").write_bytes(b"poster")
    (root / "Inbox" / "Film.mp4").write_bytes(b"another title with the same stem")
    (root / "Show.mp4").write_bytes(b"show-bytes")
    monkeypatch.setattr(media, "probe", lambda path: {"duration": 60.0, "video": True, "tracks": []})
    items, _ = media.scan(str(root))
    library = {"id": "abc", "name": "Movies", "folder": str(root), "owner": OWNER,
               "shared_with": [VIEWER], "encoder": "cpu", "pages": ["page:abc:1"], "count": len(items)}
    docs["index"] = {"ids": ["abc"]}
    docs["library:abc"] = library
    docs["page:abc:1"] = items
    return {item["path"]: item for item in items}


def _catalog(client):
    return {item["id"]: item for item in client.get("/api/media-center/abc/items").json()["items"]}


def test_a_move_renames_the_file_and_its_sidecars_and_keeps_the_id(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    film = by_path["Inbox/Film.mkv"]

    response = client.post("/api/media-center/abc/move",
                           json={"items": [film["id"]], "folder": "Movies/Drama", "create": True})
    assert response.status_code == 200, response.text
    assert response.json()["moved"] == [film["id"]] and response.json()["errors"] == []

    dest = root / "Movies" / "Drama"
    assert (dest / "Film.mkv").read_bytes() == b"film-bytes"
    assert (dest / "Film.en.srt").is_file() and (dest / "Film.jpg").is_file()
    assert not (root / "Inbox" / "Film.mkv").exists()
    # A different TITLE sharing the stem is not a sidecar and stays where it was.
    assert (root / "Inbox" / "Film.mp4").read_bytes() == b"another title with the same stem"
    assert not (dest / "Film.mp4").exists()

    moved = _catalog(client)[film["id"]]                  # same id
    assert moved["folder"] == "Movies/Drama"
    # The moved title still plays: its catalog path is the file's new home.
    assert client.post(f"/api/media-center/abc/play/{film['id']}").status_code == 200

    # A rescan recognises the moved file (same size + mtime) and keeps the entry and its id,
    # without probing it again.
    catalog = [item for key in docs["library:abc"]["pages"] for item in docs[key]]
    probed = []
    monkeypatch.setattr(media, "probe", lambda path: probed.append(path) or {"duration": 1.0, "video": True})
    rescanned, _ = media.scan(str(root), catalog)
    again = {item["path"]: item for item in rescanned}
    assert again["Movies/Drama/Film.mkv"]["id"] == film["id"]
    assert probed == []


def test_moving_to_the_top_level_and_into_an_existing_folder(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    film, show = by_path["Inbox/Film.mkv"], by_path["Show.mp4"]
    assert client.post("/api/media-center/abc/move",
                       json={"items": [film["id"]], "folder": "."}).json()["moved"] == [film["id"]]
    assert (root / "Film.mkv").is_file()
    assert client.post("/api/media-center/abc/move",
                       json={"items": [show["id"]], "folder": "Inbox"}).json()["moved"] == [show["id"]]
    assert (root / "Inbox" / "Show.mp4").is_file()
    catalog = _catalog(client)
    assert catalog[film["id"]]["folder"] == "." and catalog[show["id"]]["folder"] == "Inbox"


def test_a_move_never_overwrites_a_file_already_there(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    (root / "Taken").mkdir()
    (root / "Taken" / "Film.mkv").write_bytes(b"somebody else's film")
    film = by_path["Inbox/Film.mkv"]
    body = client.post("/api/media-center/abc/move", json={"items": [film["id"]], "folder": "Taken"}).json()
    assert body["moved"] == [] and "already exists" in body["errors"][0]["error"]
    assert (root / "Taken" / "Film.mkv").read_bytes() == b"somebody else's film"
    assert (root / "Inbox" / "Film.mkv").read_bytes() == b"film-bytes"
    assert _catalog(client)[film["id"]]["folder"] == "Inbox"


def test_a_move_cannot_leave_the_library(api, monkeypatch, tmp_path_factory):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    outside = tmp_path_factory.mktemp("elsewhere")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "Private").mkdir()
    (root / "Private" / ".ignore").touch()
    film = by_path["Inbox/Film.mkv"]
    for folder, create in (("..", True), ("/tmp", True), ("Inbox/../../x", True), ("escape", False),
                           ("escape/new", True), ("Private", False), ("Nope", False), (".hidden", True)):
        response = client.post("/api/media-center/abc/move",
                               json={"items": [film["id"]], "folder": folder, "create": create})
        assert response.status_code == 400, (folder, response.text)
    assert (root / "Inbox" / "Film.mkv").is_file()
    assert os.listdir(outside) == []
    assert not (root / "Nope").exists()


def test_only_the_owning_admin_can_move_and_never_mid_scan(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    film = by_path["Inbox/Film.mkv"]
    body = {"items": [film["id"]], "folder": "Moved", "create": True}

    user.nostr_npub, user.is_admin = VIEWER, False                   # a viewer it is shared with
    assert client.post("/api/media-center/abc/move", json=body).status_code == 403
    user.nostr_npub, user.is_admin = OWNER, True

    routes._scans["abc"] = {"state": "running", "count": 0}
    assert client.post("/api/media-center/abc/move", json=body).status_code == 409
    routes._scans.clear()

    assert client.post("/api/media-center/abc/move", json={"items": [], "folder": "x"}).status_code == 400
    assert (root / "Inbox" / "Film.mkv").is_file()
    assert client.post("/api/media-center/abc/move", json=body).json()["moved"] == [film["id"]]


def test_the_move_targets_are_the_real_folders_even_empty_ones(api, monkeypatch):
    client, docs, user, root = api
    _library(docs, root, monkeypatch)
    (root / "Empty" / "Deeper").mkdir(parents=True)
    (root / ".cache").mkdir()
    (root / "Private" / "Inside").mkdir(parents=True)
    (root / "Private" / ".ignore").touch()
    (root / "link").symlink_to(root / "Inbox", target_is_directory=True)
    folders = client.get("/api/media-center/abc/move-targets").json()["folders"]
    assert folders == [".", "Empty", "Empty/Deeper", "Inbox"], folders
    user.nostr_npub, user.is_admin = VIEWER, False
    assert client.get("/api/media-center/abc/move-targets").status_code == 403



@pytest.fixture(autouse=True)
def _fresh_move_state():
    routes._moving.clear()
    routes._move_locks.clear()
    yield
    routes._moving.clear()
    routes._move_locks.clear()


def test_another_titles_sidecars_stay_behind(api, monkeypatch):
    """Scene-style names: `The.Matrix.Reloaded.en.srt` STARTS WITH `The.Matrix.` but is the
    sequel's. Only this title's own subtitles, poster and .nfo go with it."""
    client, docs, user, root = api
    (root / "The.Matrix.mkv").write_bytes(b"one")
    (root / "The.Matrix.Reloaded.mkv").write_bytes(b"two")
    for name in ("The.Matrix.en.srt", "The.Matrix.en.forced.srt", "The.Matrix.jpg", "The.Matrix.nfo",
                 "The.Matrix.Reloaded.en.srt", "The.Matrix.Reloaded.jpg", "The.Matrix.Reloaded.nfo"):
        (root / name).write_text(name)
    monkeypatch.setattr(media, "probe", lambda path: {"duration": 60.0, "video": True, "tracks": []})
    items, _ = media.scan(str(root))
    docs.update({"index": {"ids": ["abc"]}, "page:abc:1": items, "library:abc": {
        "id": "abc", "name": "M", "folder": str(root), "owner": OWNER, "shared_with": [],
        "encoder": "cpu", "pages": ["page:abc:1"], "count": len(items)}})
    first = next(i for i in items if i["path"] == "The.Matrix.mkv")
    assert client.post("/api/media-center/abc/move", json={"items": [first["id"]], "folder": "One", "create": True}).json()["moved"]
    assert sorted(os.listdir(root / "One")) == ["The.Matrix.en.forced.srt", "The.Matrix.en.srt", "The.Matrix.jpg",
                                               "The.Matrix.mkv", "The.Matrix.nfo"]
    assert (root / "The.Matrix.Reloaded.en.srt").is_file() and (root / "The.Matrix.Reloaded.jpg").is_file()


def test_a_new_file_at_a_moved_titles_old_path_gets_its_own_id(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    film = by_path["Inbox/Film.mkv"]
    client.post("/api/media-center/abc/move", json={"items": [film["id"]], "folder": "Moved", "create": True})
    (root / "Inbox" / "Film.mkv").write_bytes(b"a different film, same name")
    catalog = [item for key in docs["library:abc"]["pages"] for item in docs[key]]
    rescanned, _ = media.scan(str(root), catalog)
    ids = [item["id"] for item in rescanned]
    assert len(ids) == len(set(ids)), "two titles share one id"
    by_new = {item["path"]: item for item in rescanned}
    assert by_new["Moved/Film.mkv"]["id"] == film["id"]
    assert by_new["Inbox/Film.mkv"]["id"] != film["id"]


def test_a_move_does_not_mark_an_interrupted_scan_finished(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    docs["library:abc"]["scan_incomplete"] = True
    client.post("/api/media-center/abc/move", json={"items": [by_path["Show.mp4"]["id"]], "folder": "Inbox"})
    assert docs["library:abc"]["scan_incomplete"] is True


def test_no_scan_starts_under_a_move(api, monkeypatch):
    client, docs, user, root = api
    _library(docs, root, monkeypatch)
    routes._moving.add("abc")
    assert client.post("/api/media-center/abc/scan").status_code == 409
    routes._moving.discard("abc")
    assert client.post("/api/media-center/abc/scan").status_code == 200


def test_two_moves_at_once_both_land_in_the_catalog(api, monkeypatch):
    """Each move reads the catalog and saves it whole. Unserialised, the second save was built from
    a read taken before the first save and put the first title back where it no longer is."""
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    film, show = by_path["Inbox/Film.mkv"], by_path["Show.mp4"]
    real = media.move_items

    def slow(*args, **kwargs):              # both moves read the catalog before either saves
        import time
        time.sleep(.2)
        return real(*args, **kwargs)
    monkeypatch.setattr(media, "move_items", slow)

    async def both():
        return await asyncio.gather(
            routes.move_items("abc", routes.MoveItems(items=[film["id"]], folder="A", create=True), user),
            routes.move_items("abc", routes.MoveItems(items=[show["id"]], folder="B", create=True), user))
    asyncio.run(both())
    catalog = {item["id"]: item for key in docs["library:abc"]["pages"] for item in docs[key]}
    assert catalog[film["id"]]["folder"] == "A" and catalog[show["id"]]["folder"] == "B", catalog
    assert (root / "A" / "Film.mkv").is_file() and (root / "B" / "Show.mp4").is_file()
