"""Files → New folder / Move / Delete / save, against a REAL directory: what lands on disk, and what never can.

These routes (`/api/storage/mkdir`, `/move-files`, `/delete-files-bulk`, `/save-text-file`) turn a path the client
sends into a disk operation in the user's own storage, and no test had ever called them (a route-coverage scan,
2026-10-24). They are what Files' New folder / Move / multi-select Delete reach, through /api/files/*.

Found by writing them: a path that names NOTHING ("", "/", ".", "./") resolved to the user's storage ROOT -- which
is "inside the user's directory", so it passed the guard -- and Delete then `rmtree`'d the user's entire storage.
One empty string in a selection was enough.
"""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def disk(tmp_path, monkeypatch):
    from app.main import app
    from app.auth import get_current_user_optional
    from app.database import get_db
    from app.services import storage_service
    base = tmp_path / "uploads"
    base.mkdir()
    monkeypatch.setattr(storage_service.StorageService, "_load_settings",
                        lambda self: setattr(self, "upload_path", str(base)))
    who = SimpleNamespace(username="alice", id=1, is_admin=False)
    app.dependency_overrides[get_current_user_optional] = lambda: who
    app.dependency_overrides[get_db] = lambda: None
    root = base / "alice"
    (root / "Docs").mkdir(parents=True)
    (root / "Docs" / "a.txt").write_text("A")
    (root / "Docs" / "b.txt").write_text("B")
    (root / "Photos").mkdir()
    (root / "Photos" / "p.jpg").write_bytes(b"jpg")
    (base / "bob").mkdir()
    (base / "bob" / "secret.txt").write_text("bob's")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    yield TestClient(app), root, base, outside, who
    app.dependency_overrides.pop(get_current_user_optional, None)
    app.dependency_overrides.pop(get_db, None)


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_new_folder_is_made_on_disk_and_never_outside_the_users_storage(disk):
    client, root, base, outside, _ = disk
    r = client.post("/api/storage/mkdir", data={"username": "alice", "path": "Docs/2024/Receipts"})
    assert r.status_code == 200, r.text
    assert (root / "Docs" / "2024" / "Receipts").is_dir()
    assert client.post("/api/storage/mkdir", data={"username": "alice", "path": "Docs"}).status_code == 400  # exists
    for bad in ("../bob/x", "Docs/../../bob/x"):
        r = client.post("/api/storage/mkdir", data={"username": "alice", "path": bad})
        assert r.status_code in (400, 403), (bad, r.status_code)
    assert not (base / "bob" / "x").exists()
    # A leading slash is not an absolute path here: it is made INSIDE the user's own storage.
    assert client.post("/api/storage/mkdir", data={"username": "alice", "path": "/etc/x"}).status_code == 200
    assert (root / "etc" / "x").is_dir()
    os.symlink(outside, root / "link-out")
    r = client.post("/api/storage/mkdir", data={"username": "alice", "path": "link-out/made"})
    assert r.status_code == 403 and not (outside / "made").exists(), r.text


def test_move_moves_on_disk_refuses_a_taken_name_and_never_reaches_outside(disk):
    client, root, base, outside, _ = disk
    r = client.post("/api/storage/move-files", json={"username": "alice", "file_paths": ["Docs/a.txt", "Photos/p.jpg"],
                                                      "destination": "Archive"})
    assert r.status_code == 404, "a destination that does not exist is refused"
    (root / "Archive").mkdir()
    (root / "Archive" / "b.txt").write_text("already here")
    r = client.post("/api/storage/move-files", json={"username": "alice",
                                                      "file_paths": ["Docs/a.txt", "Docs/b.txt", "Photos/p.jpg", "../bob/secret.txt"],
                                                      "destination": "Archive"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["moved"]) == ["Docs/a.txt", "Photos/p.jpg"], body
    assert (root / "Archive" / "a.txt").read_text() == "A" and (root / "Archive" / "p.jpg").exists()
    assert (root / "Archive" / "b.txt").read_text() == "already here", "a taken name was overwritten"
    assert (root / "Docs" / "b.txt").read_text() == "B"
    assert (base / "bob" / "secret.txt").read_text() == "bob's"
    assert any("secret.txt" in e for e in body["errors"])
    for dest in ("../bob", "/tmp"):        # "/tmp" means alice's own (missing) tmp folder, never the host's
        r = client.post("/api/storage/move-files", json={"username": "alice", "file_paths": ["Docs/b.txt"], "destination": dest})
        assert r.status_code in (400, 403, 404), (dest, r.status_code)
    assert (root / "Docs" / "b.txt").exists()


def test_bulk_delete_deletes_what_was_picked_and_nothing_else(disk):
    client, root, base, outside, _ = disk
    os.symlink(outside, root / "link-out")
    r = client.post("/api/storage/delete-files-bulk", json={"username": "alice",
                                                            "file_paths": ["Docs/a.txt", "Photos", "../bob/secret.txt", "link-out/keep.txt"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == ["Docs/a.txt", "Photos"], body
    assert not (root / "Docs" / "a.txt").exists() and not (root / "Photos").exists()
    assert (root / "Docs" / "b.txt").exists()
    assert (base / "bob" / "secret.txt").exists() and (outside / "keep.txt").exists()


@pytest.mark.parametrize("nothing", ["", "/", ".", "./", "//", " "])
def test_a_path_that_names_nothing_never_deletes_the_users_whole_storage(disk, nothing):
    client, root, base, outside, _ = disk
    before = _tree(root)
    r = client.post("/api/storage/delete-files-bulk", json={"username": "alice", "file_paths": [nothing]})
    assert root.is_dir() and _tree(root) == before, ("deleting %r removed the user's files" % nothing, _tree(root))
    assert r.status_code in (200, 400), r.status_code
    if r.status_code == 200:
        assert r.json()["deleted"] == [] and r.json()["errors"], r.json()


@pytest.mark.parametrize("nothing", ["", "/", ".", "./", " "])
def test_deleting_one_item_that_names_nothing_never_deletes_the_users_whole_storage(disk, nothing):
    client, root, base, outside, _ = disk
    before = _tree(root)
    r = client.delete("/api/storage/delete-file", params={"username": "alice", "file_path": nothing})
    assert root.is_dir() and _tree(root) == before, ("deleting %r removed the user's files" % nothing, _tree(root))
    assert r.status_code == 400, r.status_code


def test_deleting_one_item_deletes_it(disk):
    client, root, base, outside, _ = disk
    r = client.delete("/api/storage/delete-file", params={"username": "alice", "file_path": "Docs/a.txt"})
    assert r.status_code == 200 and not (root / "Docs" / "a.txt").exists() and (root / "Docs" / "b.txt").exists(), r.text


@pytest.mark.parametrize("nothing", ["", "/", "."])
def test_a_path_that_names_nothing_is_never_moved(disk, nothing):
    client, root, base, outside, _ = disk
    (root / "Archive").mkdir()
    before = _tree(root)
    r = client.post("/api/storage/move-files", json={"username": "alice", "file_paths": [nothing], "destination": "Archive"})
    assert _tree(root) == before, ("moving %r moved the whole storage" % nothing, _tree(root))
    if r.status_code == 200:
        assert r.json()["moved"] == [], r.json()


def test_saving_a_text_file_writes_it_and_never_outside(disk):
    client, root, base, outside, _ = disk
    r = client.post("/api/storage/save-text-file", data={"username": "alice", "path": "Calendars/home.ics", "content": "BEGIN:VCALENDAR"})
    assert r.status_code == 200 and (root / "Calendars" / "home.ics").read_text() == "BEGIN:VCALENDAR", r.text
    for bad in ("../bob/secret.txt", "Docs/../../bob/secret.txt"):
        r = client.post("/api/storage/save-text-file", data={"username": "alice", "path": bad, "content": "x"})
        assert r.status_code in (400, 403), (bad, r.status_code)
    assert (base / "bob" / "secret.txt").read_text() == "bob's"
    # A leading slash stays inside her storage: never the host's /etc/passwd.
    assert client.post("/api/storage/save-text-file", data={"username": "alice", "path": "/etc/passwd", "content": "x"}).status_code == 200
    assert (root / "etc" / "passwd").read_text() == "x"


def test_another_users_storage_and_no_session_are_refused(disk):
    client, root, base, outside, who = disk
    for call in (lambda: client.post("/api/storage/mkdir", data={"username": "bob", "path": "x"}),
                 lambda: client.post("/api/storage/delete-files-bulk", json={"username": "bob", "file_paths": ["secret.txt"]}),
                 lambda: client.post("/api/storage/move-files", json={"username": "bob", "file_paths": ["secret.txt"], "destination": ""}),
                 lambda: client.post("/api/storage/save-text-file", data={"username": "bob", "path": "secret.txt", "content": "x"})):
        assert call().status_code == 403
    assert (base / "bob" / "secret.txt").read_text() == "bob's"
    from app.main import app
    from app.auth import get_current_user_optional
    app.dependency_overrides[get_current_user_optional] = lambda: None
    assert client.post("/api/storage/delete-files-bulk", json={"username": "alice", "file_paths": ["Docs/a.txt"]}).status_code == 401
    assert (root / "Docs" / "a.txt").exists()
