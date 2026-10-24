"""Media Center → Move folder: a whole folder moves on disk, and every title inside keeps its id.

Reported 2026-10-24: "Media center -> No way to move folders. Moving files support only". The shipped endpoint
against a real temporary library folder:

  * the directory is renamed on disk, with everything inside it (sidecars, subfolders, folder art);
  * every title under it keeps its ID, with its path and folder rewritten, so watch progress survives and the
    next rescan reuses the entries instead of re-probing them;
  * it cannot go into itself or one of its own subfolders, over a name already taken, out of the library, or
    be the library's top level; it is refused while the library scans, and only the owner may do it.
"""
from app.routers import media_center as routes
from app.services import media_center as media
from tests import test_media_center as _shared
from tests.test_media_center_move import _catalog, _library

api = _shared.api


def _library_with_subfolder(docs, root, monkeypatch):
    """The shared library, plus a subfolder holding a title and folder art, rescanned into the catalog."""
    _library(docs, root, monkeypatch)
    (root / "Inbox" / "Extras").mkdir()
    (root / "Inbox" / "Extras" / "Trailer.mp4").write_bytes(b"trailer")
    (root / "Inbox" / "folder.png").write_bytes(b"art")
    items, _ = media.scan(str(root))
    docs["page:abc:1"] = items
    docs["library:abc"] = {**docs["library:abc"], "count": len(items)}
    return {item["path"]: item for item in items}


def test_a_folder_moves_on_disk_with_everything_in_it_and_every_title_keeps_its_id(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library_with_subfolder(docs, root, monkeypatch)
    before = {p: item["id"] for p, item in by_path.items() if p.startswith("Inbox/")}
    assert "Inbox/Extras/Trailer.mp4" in before

    r = client.post("/api/media-center/abc/move-folder", json={"folder": "Inbox", "to": "Movies/2024", "create": True})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "Movies/2024/Inbox" and r.json()["moved"] == len(before)

    dest = root / "Movies" / "2024" / "Inbox"
    assert not (root / "Inbox").exists()
    assert (dest / "Film.mkv").read_bytes() == b"film-bytes"
    assert (dest / "Film.en.srt").is_file() and (dest / "Film.jpg").is_file() and (dest / "folder.png").is_file()
    assert (dest / "Extras" / "Trailer.mp4").read_bytes() == b"trailer"

    catalog = _catalog(client)                                     # what a viewer is served
    stored = {item["id"]: item for key in docs["library:abc"]["pages"] for item in docs[key]}
    for old_path, item_id in before.items():
        assert stored[item_id]["path"] == "Movies/2024/" + old_path, stored[item_id]   # the SAME id
        assert catalog[item_id]["folder"] == ("Movies/2024/Inbox/Extras" if "Extras" in old_path else "Movies/2024/Inbox")
    # A title outside the folder is untouched.
    assert stored[by_path["Show.mp4"]["id"]]["path"] == "Show.mp4"
    # Moved titles still play from their new home.
    assert client.post(f"/api/media-center/abc/play/{by_path['Inbox/Film.mkv']['id']}").status_code == 200

    # A rescan recognises every moved file and reprobes none of them.
    stored = [item for key in docs["library:abc"]["pages"] for item in docs[key]]
    probed = []
    monkeypatch.setattr(media, "probe", lambda path: probed.append(path) or {"duration": 1.0, "video": True})
    rescanned, _ = media.scan(str(root), stored)
    again = {item["path"]: item["id"] for item in rescanned}
    for old_path, item_id in before.items():
        assert again["Movies/2024/" + old_path] == item_id
    assert probed == []


def test_a_folder_cannot_go_into_itself_over_a_taken_name_or_out_of_the_library(api, monkeypatch):
    client, docs, user, root = api
    _library_with_subfolder(docs, root, monkeypatch)
    (root / "Shelf" / "Inbox").mkdir(parents=True)

    def refused(folder, to, create=False):
        r = client.post("/api/media-center/abc/move-folder", json={"folder": folder, "to": to, "create": create})
        assert r.status_code == 400, (folder, to, r.status_code, r.text)
        return r.json()["detail"]

    assert "into itself" in refused("Inbox", "Inbox")
    assert "into itself" in refused("Inbox", "Inbox/Extras")
    assert "already exists" in refused("Inbox", "Shelf")
    assert "top level" in refused(".", "Shelf")
    assert "already in that folder" in refused("Inbox/Extras", "Inbox")
    assert refused("Inbox", "../elsewhere", True)
    assert refused("../", "Shelf")
    # Nothing moved.
    assert (root / "Inbox" / "Film.mkv").is_file() and (root / "Inbox" / "Extras" / "Trailer.mp4").is_file()


def test_a_folder_move_waits_for_a_scan_and_is_the_owners_alone(api, monkeypatch):
    client, docs, user, root = api
    _library(docs, root, monkeypatch)
    monkeypatch.setitem(routes._scans, "abc", {"state": "running"})
    r = client.post("/api/media-center/abc/move-folder", json={"folder": "Inbox", "to": "Elsewhere", "create": True})
    assert r.status_code == 409 and (root / "Inbox").is_dir()
    routes._scans.pop("abc", None)

    docs["library:abc"] = {**docs["library:abc"], "owner": "someone-else"}
    r = client.post("/api/media-center/abc/move-folder", json={"folder": "Inbox", "to": "Elsewhere", "create": True})
    assert r.status_code in (403, 404) and (root / "Inbox").is_dir(), r.status_code
