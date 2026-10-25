"""Files → an external storage mount: a user given ONE mount reaches nothing beside it.

The mount check was `str(resolved).startswith(str(mount_path))` -- a STRING prefix. With a mount at `/srv/media`,
`MOUNT/../media-private/secret.txt` resolves to `/srv/media-private/secret.txt`, which starts with "/srv/media": so
listing, viewing (the file's bytes) and thumbnails of any sibling folder whose name began with the mount's were open
to every user the admin had given that mount. Found 2026-10-24 by the route-coverage pass. Real directories here.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def mount(tmp_path, monkeypatch):
    from app.main import app
    from app.auth import get_current_user, get_current_user_optional
    from app.database import get_db
    from app.routers import files
    srv = tmp_path / "srv"
    (srv / "media").mkdir(parents=True)
    (srv / "media" / "film.txt").write_text("in the mount")
    (srv / "media-private").mkdir()
    (srv / "media-private" / "secret.txt").write_text("PRIVATE-SIBLING")
    (srv / "mediaX").mkdir()
    (srv / "mediaX" / "x.txt").write_text("PRIVATE-SIBLING-2")
    row = SimpleNamespace(id=1, name="Media", mount_point="Media", mount_path=str(srv / "media"), is_active=True,
                          allowed_user_ids=[7])
    row.get = lambda k, d=None: getattr(row, k, d)

    async def active(point):
        return row if point == "Media" else None
    monkeypatch.setattr(files, "_active_mount", active)
    monkeypatch.setattr(files._es, "allows", lambda m, u: bool(m) and getattr(u, "id", None) in m.allowed_user_ids)
    from app.services import settings_store
    real_get = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, d="": "" if k == "storage_server_url" else real_get(k, d))
    me = SimpleNamespace(id=7, username="viewer", is_admin=False, nostr_npub="", storage_quota=0, storage_used=0)
    app.dependency_overrides[get_current_user] = lambda: me
    app.dependency_overrides[get_current_user_optional] = lambda: me
    app.dependency_overrides[get_db] = lambda: None
    yield TestClient(app, raise_server_exceptions=False)
    for dep in (get_current_user, get_current_user_optional, get_db):
        app.dependency_overrides.pop(dep, None)


def _leaks(r):
    return b"PRIVATE-SIBLING" in r.content


def test_the_mount_itself_is_listed_and_its_file_viewed(mount):
    r = mount.get("/api/files/list", params={"path": "Media"})
    assert r.status_code == 200 and "film.txt" in r.text, (r.status_code, r.text[:200])
    r = mount.get("/api/files/view/Media/film.txt")
    assert r.status_code == 200 and r.content == b"in the mount", (r.status_code, r.text[:200])


@pytest.mark.parametrize("sibling", ["../media-private", "../mediaX", "sub/../../media-private"])
def test_a_sibling_folder_is_never_listed(mount, sibling):
    r = mount.get("/api/files/list", params={"path": "Media/" + sibling})
    assert r.status_code in (400, 403, 404) and "secret.txt" not in r.text and "x.txt" not in r.text, \
        (sibling, r.status_code, r.text[:200])


@pytest.mark.parametrize("route", ["view", "thumbnail"])
@pytest.mark.parametrize("target", ["%2e%2e/media-private/secret.txt", "%2e%2e/mediaX/x.txt"])
def test_a_sibling_file_is_never_served(mount, route, target):
    r = mount.get("/api/files/%s/Media/%s" % (route, target))
    assert not _leaks(r), (route, target, r.status_code)
