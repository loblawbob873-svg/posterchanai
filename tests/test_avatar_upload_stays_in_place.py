"""An avatar upload writes ONE image file, inside the user's own folder -- whatever the upload's filename says.

Found 2026-10-24 by the route-coverage scan (no test had ever called /api/storage/save-avatar): the "extension" was
`filename.split('.')[-1]`, taken from the upload as sent, and the file was written to `user_dir / f"avatar{ext}"`.
A filename of `a.png/../../../x` therefore wrote the uploaded bytes OUTSIDE the user's folder, anywhere the service
account can write -- reachable by any signed-in account, since the route checks only that the username is theirs.
The upload had no size limit either.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture()
def disk(tmp_path, monkeypatch):
    from app.main import app
    from app.auth import get_current_user
    from app.database import get_db
    from app.services import storage_service
    base = tmp_path / "srv" / "uploads"
    base.mkdir(parents=True)
    monkeypatch.setattr(storage_service.StorageService, "_load_settings",
                        lambda self: setattr(self, "upload_path", str(base)))
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="alice", id=1, is_admin=False)
    app.dependency_overrides[get_db] = lambda: None
    yield TestClient(app), base, tmp_path
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def _files_outside(tmp_path, base):
    return sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
                  if p.is_file() and base not in p.parents)


def test_an_avatar_is_saved_as_one_image_in_the_users_folder(disk):
    client, base, tmp_path = disk
    r = client.post("/api/storage/save-avatar", data={"username": "alice"},
                    files={"file": ("me.PNG", PNG, "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["filename"] == "avatar.png"
    assert (base / "alice" / "avatar.png").read_bytes() == PNG


@pytest.mark.parametrize("name", [
    "a.png/../../../evil.txt",
    "a.png/../../../../srv/evil",
    "x./../escape",
    "../../avatar.png",
])
def test_a_filename_cannot_put_the_upload_anywhere_else(disk, name):
    client, base, tmp_path = disk
    r = client.post("/api/storage/save-avatar", data={"username": "alice"},
                    files={"file": (name, PNG, "image/png")})
    assert _files_outside(tmp_path, base) == [], ("an avatar upload wrote outside the user's folder", name, r.status_code)
    written = [p for p in (base / "alice").glob("*")] if (base / "alice").exists() else []
    assert all(p.name in ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.gif", "avatar.webp") for p in written), written


@pytest.mark.parametrize("name", ["shell.php", "x.html", "noext", "a.svg"])
def test_only_an_image_extension_is_ever_written(disk, name):
    client, base, tmp_path = disk
    client.post("/api/storage/save-avatar", data={"username": "alice"}, files={"file": (name, PNG, "image/png")})
    written = [p.name for p in (base / "alice").glob("*")] if (base / "alice").exists() else []
    assert all(n in ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.gif", "avatar.webp") for n in written), written


def test_an_oversized_avatar_is_refused(disk):
    client, base, tmp_path = disk
    from app.services import storage_service
    big = PNG + b"\x00" * (storage_service.AVATAR_MAX_BYTES + 1)
    r = client.post("/api/storage/save-avatar", data={"username": "alice"}, files={"file": ("big.png", big, "image/png")})
    assert r.status_code == 413, r.status_code
    assert not (base / "alice" / "avatar.png").exists()


def test_another_users_avatar_is_refused(disk):
    client, base, tmp_path = disk
    r = client.post("/api/storage/save-avatar", data={"username": "bob"}, files={"file": ("me.png", PNG, "image/png")})
    assert r.status_code == 403 and not (base / "bob").exists()


def test_the_profile_avatar_upload_refuses_an_oversized_file_before_forwarding_it(disk, monkeypatch):
    """/api/auth/avatar holds the whole upload in memory and forwards it; it used to read with no limit."""
    client, base, tmp_path = disk
    from app.services import settings_store, storage_service
    monkeypatch.setattr(settings_store, "get", lambda k, d="": "http://storage.test" if k == "storage_server_url" else d)
    forwarded = []

    async def proxy(**kw):
        forwarded.append(kw)
        return {"filename": "avatar.png"}
    import app.services.storage_proxy as sp
    monkeypatch.setattr(sp, "proxy_storage_request", proxy)
    big = PNG + b"\x00" * (storage_service.AVATAR_MAX_BYTES + 1)
    r = client.post("/api/auth/avatar", files={"file": ("big.png", big, "image/png")})
    assert r.status_code == 413 and forwarded == [], (r.status_code, len(forwarded))
