"""The routes that READ a file by a name the client sends serve only what belongs there.

Companion to tests/test_storage_file_ops_on_disk.py and tests/test_avatar_upload_stays_in_place.py, which found three
writes reaching outside the user's folder on 2026-10-24. These pin the read side, which had no test: Files' view and
thumbnail (`/api/storage/view-file`, `/thumbnail-file`), a user's avatar by name, and the Meme Builder's sound preview,
against `..` in every spelling, a symlink pointing out, and another user's folder.
"""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def disk(tmp_path, monkeypatch):
    from app.main import app
    from app.auth import get_current_user, get_current_user_optional
    from app.database import get_db
    from app.services import storage_service
    base = tmp_path / "uploads"
    (base / "alice" / "Docs").mkdir(parents=True)
    (base / "alice" / "Docs" / "mine.txt").write_text("alice's")
    (base / "bob").mkdir()
    (base / "bob" / "secret.txt").write_text("bob's secret")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host.txt").write_text("host file")
    os.symlink(outside, base / "alice" / "link-out")
    monkeypatch.setattr(storage_service.StorageService, "_load_settings",
                        lambda self: setattr(self, "upload_path", str(base)))
    alice = SimpleNamespace(username="alice", id=1, is_admin=False)
    app.dependency_overrides[get_current_user] = lambda: alice
    app.dependency_overrides[get_current_user_optional] = lambda: alice
    app.dependency_overrides[get_db] = lambda: None
    yield TestClient(app), base
    for dep in (get_current_user, get_current_user_optional, get_db):
        app.dependency_overrides.pop(dep, None)


def _leaked(r):
    return r.status_code == 200 and any(s in r.content for s in (b"bob's secret", b"host file"))


def test_view_serves_the_users_own_file(disk):
    client, base = disk
    r = client.get("/api/storage/view-file", params={"username": "alice", "file_path": "Docs/mine.txt"})
    assert r.status_code == 200 and r.content == b"alice's", r.status_code


@pytest.mark.parametrize("path", [
    "../bob/secret.txt", "Docs/../../bob/secret.txt", "%2e%2e/bob/secret.txt", "%252e%252e/bob/secret.txt",
    "..\\bob\\secret.txt", "/../bob/secret.txt", "link-out/host.txt", "link-out/../../outside/host.txt",
])
def test_view_and_thumbnail_never_serve_another_users_file_or_the_host(disk, path):
    client, base = disk
    for route in ("/api/storage/view-file", "/api/storage/thumbnail-file"):
        r = client.get(route, params={"username": "alice", "file_path": path})
        assert not _leaked(r), (route, path, r.status_code)


def test_another_users_files_are_refused_by_name(disk):
    client, base = disk
    r = client.get("/api/storage/view-file", params={"username": "bob", "file_path": "secret.txt"})
    assert r.status_code == 403 and not _leaked(r)


@pytest.mark.parametrize("who", ["../bob", "..", "bob/../alice", "%2e%2e"])
def test_an_avatar_by_name_never_reaches_outside_the_uploads(disk, who):
    client, base = disk
    (base / "bob" / "avatar.png").write_bytes(b"\x89PNG bob")
    r = client.get("/api/auth/avatar/" + who)
    assert not (r.status_code == 200 and r.content == b"\x89PNG bob" and who != "bob"), (who, r.status_code)
    assert not _leaked(r)


@pytest.mark.parametrize("name", ["..%2F..%2Fapp%2Fmain.py", "../../etc/passwd", "a.b", "%2e%2e"])
def test_the_meme_sound_preview_serves_only_a_named_sound(disk, name):
    client, base = disk
    r = client.get("/client/meme/sound/" + name)
    assert r.status_code == 404, (name, r.status_code)
