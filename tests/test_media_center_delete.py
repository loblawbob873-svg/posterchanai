"""Media Center → Delete: an admin removes a title, and the FILE is removed from disk.

Reported as "Media Center -> Add ability for admins to delete files. Make sure removed from disk."
These run the shipped endpoint against a real temporary library folder:

  * the file, and only ITS subtitles/poster, are gone from disk; the catalog forgets the title and
    the transcoded segments cached for it are dropped;
  * only the owning admin can do it -- a viewer it is shared with, a non-admin owner and an admin
    it is merely shared with all get 403 and the file is still there;
  * the client names an id, never a path, and a catalog entry that points out of the library
    (a symlinked folder, a symlinked file, `..`, an absolute path) or at a folder is refused with
    nothing outside touched;
  * a file already gone is "already removed", and a refusal from the filesystem is a sentence;
  * on a frontend node the request goes to the node that holds the files, authenticated, and
    nothing local is touched.
"""
import logging
import os

import pytest

from app.routers import media_center as routes
from app.services import media_center as media
from tests import test_media_center as _shared
from tests.test_media_center import OWNER, VIEWER

api = _shared.api


@pytest.fixture(autouse=True)
def _fresh_state():
    routes._moving.clear()
    routes._move_locks.clear()
    yield
    routes._moving.clear()
    routes._move_locks.clear()


def _library(docs, root, monkeypatch, extra=()):
    (root / "Inbox").mkdir()
    (root / "Inbox" / "Film.mkv").write_bytes(b"film-bytes")
    (root / "Inbox" / "Film.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    (root / "Inbox" / "Film.jpg").write_bytes(b"poster")
    (root / "Inbox" / "Film.mp4").write_bytes(b"another title with the same stem")
    (root / "Inbox" / "Film.Two.en.srt").write_text("the sequel's subtitles")
    (root / "Inbox" / "Film.one.mkv").write_bytes(b"a title whose stem looks like Film + a tag")
    (root / "Inbox" / "Film.one.srt").write_text("its subtitles")
    (root / "Show.mp4").write_bytes(b"show-bytes")
    monkeypatch.setattr(media, "probe", lambda path: {"duration": 13.0, "video": True, "tracks": []})
    items, _ = media.scan(str(root))
    items += list(extra)
    library = {"id": "abc", "name": "Movies", "folder": str(root), "owner": OWNER,
               "shared_with": [VIEWER], "encoder": "cpu", "pages": ["page:abc:1"], "count": len(items)}
    docs["index"] = {"ids": ["abc"]}
    docs["library:abc"] = library
    docs["page:abc:1"] = items
    return {item["path"]: item for item in items}


def _ids(client):
    return {item["id"] for item in client.get("/api/media-center/abc/items").json()["items"]}


def _committed(docs):
    return {item["id"] for key in docs["library:abc"]["pages"] for item in docs[key]}


def test_an_admin_deletes_a_title_and_it_is_gone_from_disk_and_catalog(api, monkeypatch, caplog):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    film = by_path["Inbox/Film.mkv"]
    # A segment this node transcoded for it earlier.
    _, cache, _, segment = media.segment_cache_location(docs["library:abc"], film, "480p", 1)
    segment.write_bytes(b"ts")
    other = media.segment_cache_location(docs["library:abc"], by_path["Show.mp4"], "480p", 1)[3]
    other.write_bytes(b"ts")

    with caplog.at_level(logging.INFO):
        response = client.delete(f"/api/media-center/abc/items/{film['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "deleted" and body["deleted"] == film["id"]
    assert "Deleted Film.mkv from disk" in body["message"]

    assert not (root / "Inbox" / "Film.mkv").exists()
    assert not (root / "Inbox" / "Film.en.srt").exists() and not (root / "Inbox" / "Film.jpg").exists()
    assert sorted(body["sidecars"]) == ["Film.en.srt", "Film.jpg"]
    # Another TITLE sharing the stem, and another title's subtitles, stay.
    assert (root / "Inbox" / "Film.mp4").read_bytes() == b"another title with the same stem"
    assert (root / "Inbox" / "Film.Two.en.srt").is_file() and (root / "Show.mp4").is_file()
    assert (root / "Inbox" / "Film.one.mkv").is_file() and (root / "Inbox" / "Film.one.srt").is_file()

    assert film["id"] not in _ids(client) and film["id"] not in _committed(docs)
    assert by_path["Show.mp4"]["id"] in _ids(client)
    assert docs["library:abc"]["count"] == len(by_path) - 1
    assert not segment.exists() and other.exists()
    assert any("deleted 'Inbox/Film.mkv'" in r.getMessage() and OWNER in r.getMessage() for r in caplog.records)
    assert not any("film-bytes" in r.getMessage() for r in caplog.records)
    # And it is not playable any more.
    assert client.post(f"/api/media-center/abc/play/{film['id']}").status_code == 404


def test_only_the_owning_admin_can_delete(api, monkeypatch):
    client, docs, user, root = api
    film = _library(docs, root, monkeypatch)["Inbox/Film.mkv"]
    url = f"/api/media-center/abc/items/{film['id']}"
    for npub, admin in ((VIEWER, False), (OWNER, False), (VIEWER, True)):
        user.nostr_npub, user.is_admin = npub, admin
        assert client.delete(url).status_code == 403, (npub, admin)
    user.nostr_npub, user.is_admin, user.can_media = VIEWER, False, False
    assert client.delete(url).status_code == 403
    assert (root / "Inbox" / "Film.mkv").read_bytes() == b"film-bytes"
    user.nostr_npub, user.is_admin, user.can_media = OWNER, True, True
    assert film["id"] in _ids(client)


def test_a_delete_can_never_reach_outside_the_library(api, monkeypatch, tmp_path_factory):
    client, docs, user, root = api
    outside = tmp_path_factory.mktemp("elsewhere")
    (outside / "secret.mkv").write_bytes(b"not yours")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "link.mkv").symlink_to(outside / "secret.mkv")
    (root / "Folder.mkv").mkdir()
    (root / "Folder.mkv" / "inside.mkv").write_bytes(b"inside")
    base = {"size": 9, "mtime_ns": 0, "duration": 13.0, "video": True, "tracks": []}
    evil = [{**base, "id": "via-dir-link", "path": "escape/secret.mkv", "name": "a", "folder": "escape"},
            {**base, "id": "via-file-link", "path": "link.mkv", "name": "b", "folder": "."},
            {**base, "id": "traversal", "path": "../" + outside.name + "/secret.mkv", "name": "c", "folder": "."},
            {**base, "id": "absolute", "path": str(outside / "secret.mkv"), "name": "d", "folder": "."},
            {**base, "id": "a-folder", "path": "Folder.mkv", "name": "e", "folder": "."},
            {**base, "id": "empty", "path": "", "name": "f", "folder": "."}]
    _library(docs, root, monkeypatch, extra=evil)
    for entry in evil:
        response = client.delete(f"/api/media-center/abc/items/{entry['id']}")
        assert response.status_code in (400, 409), (entry["id"], response.text)
        assert response.json()["detail"], entry["id"]
    assert (outside / "secret.mkv").read_bytes() == b"not yours"
    assert os.path.islink(root / "link.mkv") and os.path.islink(root / "escape")
    assert (root / "Folder.mkv" / "inside.mkv").is_file()
    assert {entry["id"] for entry in evil} <= _committed(docs)       # a refusal forgets nothing
    # An id the library does not hold is a 404, never a path lookup.
    assert client.delete("/api/media-center/abc/items/..%2F..%2Fetc").status_code == 404
    assert client.delete("/api/media-center/abc/items/nope").status_code == 404


def test_an_already_missing_file_is_already_removed(api, monkeypatch):
    client, docs, user, root = api
    by_path = _library(docs, root, monkeypatch)
    film, show = by_path["Inbox/Film.mkv"], by_path["Show.mp4"]
    (root / "Inbox" / "Film.mkv").unlink()
    body = client.delete(f"/api/media-center/abc/items/{film['id']}").json()
    assert body["status"] == "already_gone" and "already removed" in body["message"], body
    assert film["id"] not in _committed(docs)
    # The whole folder gone is the same answer.
    import shutil
    shutil.rmtree(root / "Inbox")
    (root / "Show.mp4").rename(root / "moved-away.mp4")
    assert client.delete(f"/api/media-center/abc/items/{show['id']}").json()["status"] == "already_gone"
    assert show["id"] not in _committed(docs)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_permission_denied_is_a_sentence_and_nothing_is_forgotten(api, monkeypatch):
    client, docs, user, root = api
    film = _library(docs, root, monkeypatch)["Inbox/Film.mkv"]
    os.chmod(root / "Inbox", 0o555)
    try:
        response = client.delete(f"/api/media-center/abc/items/{film['id']}")
    finally:
        os.chmod(root / "Inbox", 0o755)
    assert response.status_code == 409
    assert "permission denied" in response.json()["detail"], response.text
    assert (root / "Inbox" / "Film.mkv").is_file()
    assert film["id"] in _committed(docs)


def test_read_only_filesystem_is_a_sentence(api, monkeypatch):
    import errno
    client, docs, user, root = api
    film = _library(docs, root, monkeypatch)["Inbox/Film.mkv"]
    real_unlink = os.unlink

    def read_only(path, *args, **kwargs):
        if path == "Film.mkv":
            raise OSError(errno.EROFS, "Read-only file system")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(media.os, "unlink", read_only)
    response = client.delete(f"/api/media-center/abc/items/{film['id']}")
    assert response.status_code == 409 and "read-only filesystem" in response.json()["detail"]
    assert (root / "Inbox" / "Film.mkv").is_file() and film["id"] in _committed(docs)


def test_no_delete_while_the_library_scans(api, monkeypatch):
    client, docs, user, root = api
    film = _library(docs, root, monkeypatch)["Inbox/Film.mkv"]
    routes._scans["abc"] = {"state": "running", "count": 0}
    assert client.delete(f"/api/media-center/abc/items/{film['id']}").status_code == 409
    routes._scans.clear()
    assert (root / "Inbox" / "Film.mkv").is_file()


def test_a_frontend_forwards_the_delete_to_the_node_that_holds_the_files(api, monkeypatch):
    """With a Media Center server configured, this node holds no files for it. The delete must go to
    that node, signed with the shared secret and carrying the admin verdict -- and a local library
    folder must not be touched, even one with a matching id."""
    import httpx
    client, docs, user, root = api
    film = _library(docs, root, monkeypatch)["Inbox/Film.mkv"]
    monkeypatch.setattr(routes.settings_store, "get", lambda *args: "http://nas.lan:3051")
    monkeypatch.setattr(routes.lb_auth, "shared_secret", lambda: "test-secret")
    sent = []

    class Nas:
        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)

        async def send(self, request, **kwargs):
            sent.append(request)
            body = b'{"deleted": "x", "status": "deleted", "message": "Deleted Film.mkv from disk."}'
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))
    monkeypatch.setattr(routes, "_proxy_client", Nas())
    response = client.delete(f"/api/media-center/abc/items/{film['id']}")
    assert response.status_code == 200 and response.json()["status"] == "deleted"
    assert len(sent) == 1
    request = sent[0]
    assert request.method == "DELETE"
    assert str(request.url) == f"http://nas.lan:3051/api/media-center/abc/items/{film['id']}"
    assert request.headers["X-PC-Media-Viewer"] == OWNER and request.headers["X-PC-Media-Admin"] == "true"
    assert request.headers[routes.lb_auth.AUTH_HEADER_NAME] == "test-secret"
    assert (root / "Inbox" / "Film.mkv").read_bytes() == b"film-bytes"   # nothing local
    assert film["id"] in _committed(docs)
    # A non-admin is forwarded as a non-admin; the storage node decides (next test).
    user.is_admin = False
    client.delete(f"/api/media-center/abc/items/{film['id']}")
    assert sent[-1].headers["X-PC-Media-Admin"] == "false"


def test_the_storage_node_refuses_a_delegated_non_admin(api, monkeypatch):
    from app.auth import get_current_user_optional
    from app.utils import lb_auth
    client, docs, user, root = api
    film = _library(docs, root, monkeypatch)["Inbox/Film.mkv"]
    client.app.dependency_overrides.pop(routes.media_user_optional)
    client.app.dependency_overrides[get_current_user_optional] = lambda: None
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: "test-secret")
    headers = {"X-PC-Media-Viewer": OWNER, "X-PC-Media-Admin": "false", "X-PC-Media-Allowed": "true",
               lb_auth.FLAG_HEADER_NAME: "true", lb_auth.AUTH_HEADER_NAME: "test-secret"}
    url = f"/api/media-center/abc/items/{film['id']}"
    assert client.delete(url, headers=headers).status_code == 403
    assert (root / "Inbox" / "Film.mkv").is_file()
    headers["X-PC-Media-Admin"] = "true"
    assert client.delete(url, headers=headers).json()["status"] == "deleted"
    assert not (root / "Inbox" / "Film.mkv").exists()
