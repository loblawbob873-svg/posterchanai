import asyncio
import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import media_center as routes
from app.services import media_center as media

OWNER = "11" * 32
VIEWER = "22" * 32


@pytest.fixture
def api(monkeypatch, tmp_path):
    documents = {}
    user = SimpleNamespace(nostr_npub=OWNER, is_admin=True, can_media=True)
    async def read(key):
        return copy.deepcopy(documents.get(key))
    async def write(key, value):
        documents[key] = copy.deepcopy(value)
    monkeypatch.setattr(media, "read", read)
    monkeypatch.setattr(media, "write", write)
    monkeypatch.setenv("POSTERCHANAI_MEDIA_ROOTS", str(tmp_path))
    monkeypatch.setenv("POSTERCHANAI_MEDIA_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(media, "mutation_lock", asyncio.Lock())
    monkeypatch.setattr(media, "_job_condition", asyncio.Condition())
    routes._scans.clear()
    monkeypatch.setattr(routes.settings_store, "get", lambda *args: "")
    media._sessions.clear()
    media._catalog_cache.clear()
    media._rate_due.clear()
    media._failed_encoders.clear()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.media_user_optional] = lambda: user
    with TestClient(app) as client:
        yield client, documents, user, tmp_path


def seed(documents, folder):
    library = {"id": "abc", "name": "Movies", "folder": str(folder), "owner": OWNER,
               "shared_with": [VIEWER], "encoder": "cpu", "pages": ["page:abc:1:0"], "count": 1}
    documents["index"] = {"ids": ["abc"]}
    documents["library:abc"] = library
    documents["page:abc:1:0"] = [{"id": "movie", "duration": 13, "name": "Movie", "path": "movie.mp4", "video": True}]
    return library


def test_shared_user_playback_and_immediate_revocation(api):
    client, docs, user, folder = api
    seed(docs, folder)
    user.nostr_npub = VIEWER
    user.is_admin = False
    result = client.get("/api/media-center")
    assert result.headers["cache-control"] == "private, no-store"
    assert "folder" not in result.json()["libraries"][0]
    assert "path" not in client.get("/api/media-center/abc/items").json()["items"][0]
    assert client.post("/api/media-center/abc/scan").status_code == 403
    assert client.put("/api/media-center/abc/sharing", json={"shared_with": []}).status_code == 403
    url = client.post("/api/media-center/abc/play/movie").json()["url"]
    assert client.get(url).status_code == 200
    assert client.get(url.replace("ticket=", "ticket=bad")).status_code in (403, 422)
    docs["library:abc"]["shared_with"] = []
    assert client.get(url).status_code == 404
    assert client.get("/api/media-center").json()["libraries"] == []


@pytest.mark.parametrize("pubkey", [OWNER, VIEWER])
def test_non_admins_are_read_only_even_if_they_own_the_library(api, pubkey):
    client, docs, user, folder = api
    seed(docs, folder)
    user.nostr_npub, user.is_admin = pubkey, False
    listing = client.get("/api/media-center").json()
    assert listing["can_create"] is False
    library = listing["libraries"][0]
    assert library["can_manage"] is False
    assert "folder" not in library and "shared_with" not in library
    assert client.get("/api/media-center/abc/items").status_code == 200
    assert client.post("/api/media-center/abc/play/movie").status_code == 200
    assert client.post("/api/media-center", json={"name": "More", "folder": str(folder)}).status_code == 403
    assert client.get("/api/media-center/limits").status_code == 403
    assert client.put("/api/media-center/limits", json=media.DEFAULT_LIMITS).status_code == 403
    assert client.post("/api/media-center/abc/scan").status_code == 403
    assert client.get("/api/media-center/abc/scan").status_code == 403
    assert client.put("/api/media-center/abc/sharing", json={"shared_with": []}).status_code == 403
    assert docs["library:abc"]["shared_with"] == [VIEWER]


def test_bandwidth_caps_filter_and_reject_high_profiles(api):
    client, docs, user, folder = api
    seed(docs, folder)
    config = {**media.DEFAULT_LIMITS, "viewer_kbps": 650}
    assert client.put("/api/media-center/limits", json=config).status_code == 200
    url = client.post("/api/media-center/abc/play/movie").json()["url"]
    manifest = client.get(url)
    assert "360p.m3u8" in manifest.text and "480p" not in manifest.text
    assert client.get(url.replace("master.m3u8", "1080p-0.ts")).status_code == 404
    playlist = client.get(url.replace("master.m3u8", "360p.m3u8"))
    assert playlist.text.count("#EXTINF") == 3
    assert "#EXTINF:1.000000" in playlist.text
    assert client.get(url.replace("master.m3u8", "360p-3.ts")).status_code == 404
    assert client.put("/api/media-center/limits", json={**config, "viewer_kbps": 50000}).status_code == 400


def test_default_is_200_kilobytes_per_second(api):
    client, docs, user, folder = api
    assert client.get("/api/media-center/limits").json()["viewer_kbps"] * 1000 / 8 == 200000
    assert client.get("/api/media-center").json()["profiles"] == ["360p", "480p"]
    assert routes.Limits().viewer_kbps == media.DEFAULT_LIMITS["viewer_kbps"]


def test_stream_slots_and_expiry(api, monkeypatch):
    client, docs, user, folder = api
    seed(docs, folder)
    docs["limits"] = {**media.DEFAULT_LIMITS, "max_streams": 1}
    assert client.post("/api/media-center/abc/play/movie").status_code == 200
    user.nostr_npub = VIEWER
    assert client.post("/api/media-center/abc/play/movie").status_code == 429
    for key, (viewer, seen) in list(media._sessions.items()):
        media._sessions[key] = (viewer, seen - 91)
    assert client.post("/api/media-center/abc/play/movie").status_code == 200


def test_stopping_releases_slot_for_another_viewer(api):
    from urllib.parse import parse_qs, urlsplit
    client, docs, user, folder = api
    seed(docs, folder)
    docs["limits"] = {**media.DEFAULT_LIMITS, "max_streams": 1}
    url = client.post("/api/media-center/abc/play/movie").json()["url"]
    ticket = parse_qs(urlsplit(url).query)["ticket"][0]
    user.nostr_npub = VIEWER
    client.post("/api/media-center/sessions/stop", json={"ticket": ticket})
    assert client.post("/api/media-center/abc/play/movie").status_code == 429
    user.nostr_npub = OWNER
    client.post("/api/media-center/sessions/stop", json={"ticket": ticket})
    user.nostr_npub = VIEWER
    assert client.post("/api/media-center/abc/play/movie").status_code == 200


def test_proxy_identity_requires_secret_and_keeps_acl(api, monkeypatch):
    from app.auth import get_current_user_optional
    from app.utils import lb_auth
    client, docs, user, folder = api
    seed(docs, folder)
    client.app.dependency_overrides.pop(routes.media_user_optional)
    client.app.dependency_overrides[get_current_user_optional] = lambda: None
    headers = {"X-PC-Media-Viewer": VIEWER, "X-PC-Media-Admin": "false", "X-PC-Media-Allowed": "true", lb_auth.FLAG_HEADER_NAME: "true"}
    assert client.get("/api/media-center", headers=headers).status_code == 403
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: "test-secret")
    assert client.get("/api/media-center", headers=headers).status_code == 403
    headers[lb_auth.AUTH_HEADER_NAME] = "test-secret"
    assert len(client.get("/api/media-center", headers=headers).json()["libraries"]) == 1
    assert client.get("/api/media-center/limits", headers=headers).status_code == 403
    docs["library:abc"]["shared_with"] = []
    assert client.get("/api/media-center/abc/items", headers=headers).status_code == 404


def test_proxy_failure_never_falls_back_to_local_library(api, monkeypatch):
    import httpx
    client, docs, user, folder = api
    seed(docs, folder)
    monkeypatch.setattr(routes.settings_store, "get", lambda *args: "http://nas.lan:3051")
    monkeypatch.setattr(routes.lb_auth, "shared_secret", lambda: "test-secret")
    class Unreachable:
        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)
        async def send(self, request, **kwargs):
            assert request.headers["X-PC-Media-Viewer"] == OWNER
            assert "authorization" not in request.headers and "cookie" not in request.headers
            raise httpx.ConnectError("offline")
    monkeypatch.setattr(routes, "_proxy_client", Unreachable())
    response = client.get("/api/media-center")
    assert response.status_code == 502 and "libraries" not in response.json()
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get("/api/media-center", headers={"X-PC-Media-Hop": "1"}).status_code == 508


def test_proxy_topology_persists_on_its_node(tmp_path, monkeypatch):
    from app.services import settings_store as settings
    monkeypatch.setattr(settings, "_LOCAL_PATH", str(tmp_path / "local_settings.json"))
    monkeypatch.setattr(settings, "_CACHE", {})
    monkeypatch.setattr(settings, "_LOCAL_DIRTY", set())
    monkeypatch.setattr(settings, "_LOCAL_KEYS", set())
    monkeypatch.setattr(settings, "_loaded", False)
    settings.put("media_center_server_url", "http://nas.lan:3051")
    assert json.loads((tmp_path / "local_settings.json").read_text())["media_center_server_url"] == "http://nas.lan:3051"
    settings._CACHE.clear()
    settings._loaded = False
    settings.load_local()
    assert settings.get("media_center_server_url") == "http://nas.lan:3051"
    assert settings._is_local_only("media_center_server_url")


def test_cold_reads_restore_library_sharing_encoder_and_limits(api):
    client, docs, user, folder = api
    seed(docs, folder)["encoder"] = "amd"
    config = {**media.DEFAULT_LIMITS, "viewer_kbps": 650, "max_streams": 3}
    assert client.put("/api/media-center/limits", json=config).status_code == 200
    media._catalog_cache.clear()
    media._sessions.clear()
    assert client.get("/api/media-center/limits").json() == config
    user.nostr_npub = VIEWER
    library = client.get("/api/media-center").json()["libraries"][0]
    assert library["encoder"] == "amd"
    assert client.get("/api/media-center/abc/items").json()["items"][0]["id"] == "movie"


def test_failed_scan_preserves_catalog(api, monkeypatch):
    client, docs, user, folder = api
    library = seed(docs, folder)
    monkeypatch.setattr(media, "scan", lambda *args: ([{"id": "new"}], 0))
    async def fail(key, value):
        raise RuntimeError("relay unavailable")
    monkeypatch.setattr(media, "write", fail)
    assert client.post("/api/media-center/abc/scan").status_code == 200
    assert client.get("/api/media-center/abc/scan").json()["state"] == "failed"
    assert docs["library:abc"] == library


def test_scan_sort_reuse_and_path_confinement(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTERCHANAI_MEDIA_ROOTS", str(tmp_path))
    for name in ("Season 10/episode 1.mp4", "Season 2/episode 10.mp4", "Season 2/episode 2.mp4"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"media")
    (tmp_path / "link.mp4").symlink_to("/etc/passwd")
    monkeypatch.setattr(media, "probe", lambda path: {"duration": 12, "video": True})
    items, skipped = media.scan(str(tmp_path))
    assert [item["path"] for item in items] == ["Season 2/episode 2.mp4", "Season 2/episode 10.mp4", "Season 10/episode 1.mp4"]
    monkeypatch.setattr(media, "probe", lambda path: pytest.fail("unchanged media was probed again"))
    assert media.scan(str(tmp_path), items)[0] == items
    with pytest.raises(ValueError):
        media.safe_root("/etc")
    with pytest.raises(ValueError):
        media.source_path({"folder": str(tmp_path)}, {**items[0], "path": "../secret.mp4"})
    source = media.source_path({"folder": str(tmp_path)}, items[0])
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="rescan"):
        media.source_path({"folder": str(tmp_path)}, items[0])


def test_catalog_never_federates():
    from app.services.nostr_relay.server import _broadcastable, _private_mirrorable
    for suffix in ("index", "limits", "library:abc", "page:abc:1:0"):
        event = {"kind": 30078, "tags": [["d", media.NS + suffix]]}
        assert not _broadcastable(event, {"backup_datastore": True})
        assert not _private_mirrorable(event)


def test_real_ffmpeg_segments_cache_and_gpu_fallback(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg integration requires ffmpeg and ffprobe")
    monkeypatch.setenv("POSTERCHANAI_MEDIA_ROOTS", str(tmp_path))
    monkeypatch.setenv("POSTERCHANAI_MEDIA_CACHE", str(tmp_path / "cache"))
    source = tmp_path / "movie.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "13", "-c:v", "libx264",
                    "-threads", "1", "-c:a", "aac", str(source)], check=True, timeout=30)
    items, _ = media.scan(str(tmp_path))
    library = {"folder": str(tmp_path), "encoder": "cpu"}
    data = media.transcode(library, items[0], "360p", 1)
    segment = tmp_path / "check.ts"
    segment.write_bytes(data)
    check = media.probe(segment)
    assert check["video"] and 5.8 < check["duration"] < 6.3
    run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("cached segment transcoded again"))
    assert media.transcode(library, items[0], "360p", 1) == data
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(media, "encoder_candidates", lambda mode: ["nonexistent_encoder", "libx264"])
    assert media.transcode(library, items[0], "360p", 2)
    # Decode the complete VOD, including discontinuity boundaries and the last
    # short segment, rather than just accepting individually valid TS files.
    playlist = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6", "#EXT-X-MEDIA-SEQUENCE:0"]
    for number in range(3):
        path = tmp_path / f"part{number}.ts"
        path.write_bytes(media.transcode(library, items[0], "360p", number))
        if number:
            playlist.append("#EXT-X-DISCONTINUITY")
        playlist += [f"#EXTINF:{min(6,13-number*6)},", path.name]
    manifest = tmp_path / "test.m3u8"
    manifest.write_text("\n".join(playlist + ["#EXT-X-ENDLIST"]))
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(manifest), "-f", "null", "-"],
                             capture_output=True, timeout=30)
    assert decoded.returncode == 0, decoded.stderr.decode()


def test_transcodes_cannot_be_redirected_outside_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "source_path", lambda *args: tmp_path / "media.mp4")
    monkeypatch.setenv("POSTERCHANAI_MEDIA_CACHE", "/var/cache/media-center")
    with pytest.raises(ValueError, match="under /tmp"):
        media.transcode({"encoder": "cpu"}, {}, "360p", 0)


def test_shared_work_and_transcode_concurrency(monkeypatch):
    import threading
    import time
    async def exercise():
        monkeypatch.setattr(media, "_job_condition", asyncio.Condition())
        media._segment_jobs.clear()
        media._active_transcodes = 0
        count, active, peak = 0, 0, 0
        lock = threading.Lock()
        def encode(*args):
            nonlocal count, active, peak
            with lock:
                count += 1
                active += 1
                peak = max(peak, active)
            time.sleep(.06)
            with lock:
                active -= 1
            return b"encoded"
        monkeypatch.setattr(media, "transcode", encode)
        library = {"folder": "/media", "encoder": "cpu"}
        config = {**media.DEFAULT_LIMITS, "max_transcodes": 2}
        result = await asyncio.gather(*(media.segment(library, {"id": "a"}, "360p", n, config)
                                        for n in (0, 0, 0, 1, 2, 3)))
        assert result == [b"encoded"] * 6
        assert count == 4 and peak == 2
    asyncio.run(exercise())


def test_catalog_writes_are_encrypted_nostr_events(monkeypatch):
    from app.services import nostr_store
    from app.services.nostr import nip44
    key = bytes.fromhex("01" * 32)
    captured = []
    async def publish(port, event, **kwargs):
        captured.append(event)
        return True, ""
    monkeypatch.setattr(nostr_store, "_ws_publish", publish)
    monkeypatch.setattr(media.settings_store, "_operator_seckey", lambda db: key)
    monkeypatch.setattr(media.settings_store, "_port", lambda: 7777)
    asyncio.run(media.write("library:test", {"folder": "/private/movies", "shared_with": [VIEWER]}))
    event = captured[0]
    assert event["kind"] == 30078
    assert "/private/movies" not in event["content"] and VIEWER not in str(event["tags"])
    assert json.loads(nip44.decrypt_self(key, event["content"]))["folder"] == "/private/movies"


def test_actual_byte_pacing_shared_between_users(monkeypatch):
    async def exercise():
        media._rate_due.clear()
        monkeypatch.setattr(media, "_rate_lock", asyncio.Lock())
        config = {**media.DEFAULT_LIMITS, "server_kbps": 650, "viewer_kbps": 650}
        start = asyncio.get_running_loop().time()
        async def consume(viewer):
            return b"".join([chunk async for chunk in media.paced_bytes(b"x" * 65536, viewer, config)])
        first, second = await asyncio.gather(consume(OWNER), consume(VIEWER))
        elapsed = asyncio.get_running_loop().time() - start
        assert len(first) == len(second) == 65536
        assert elapsed >= 1.35  # 8 chunks at 650 kbps, allowing one initial chunk burst.
    asyncio.run(exercise())


def test_slow_viewer_does_not_reserve_other_users_bandwidth(monkeypatch):
    async def exercise():
        media._rate_due.clear()
        monkeypatch.setattr(media, "_rate_lock", asyncio.Lock())
        config = {**media.DEFAULT_LIMITS, "server_kbps": 20000, "viewer_kbps": 650}
        async def consume(viewer):
            return [chunk async for chunk in media.paced_bytes(b"x" * 32768, viewer, config)]
        first = asyncio.create_task(consume(OWNER))
        await asyncio.sleep(.03)
        start = asyncio.get_running_loop().time()
        stream = media.paced_bytes(b"y" * 16384, VIEWER, config)
        assert await anext(stream) == b"y" * 16384
        assert asyncio.get_running_loop().time() - start < .1
        await first
    asyncio.run(exercise())


def test_media_permission_required_and_revoked_on_existing_tickets(api):
    client, docs, user, folder = api
    seed(docs, folder)
    user.nostr_npub, user.is_admin, user.can_media = VIEWER, False, False
    assert client.get('/api/media-center').status_code == 403
    assert client.post('/api/media-center/abc/play/movie').status_code == 403
    user.can_media = True
    url = client.post('/api/media-center/abc/play/movie').json()['url']
    assert client.get(url).status_code == 200
    user.can_media = False
    assert client.get(url).status_code == 403
    user.is_admin = True
    assert client.get('/api/media-center').status_code == 200


def test_missing_login_returns_private_401(api):
    client, _, _, _ = api
    client.app.dependency_overrides[routes.media_user_optional] = lambda: None
    response = client.get('/api/media-center')
    assert response.status_code == 401
    assert response.headers['cache-control'] == 'private, no-store'


def test_permission_revocation_blocks_proxy_before_network(api, monkeypatch):
    client, docs, user, folder = api
    seed(docs, folder)
    user.nostr_npub, user.is_admin = VIEWER, False
    url = client.post('/api/media-center/abc/play/movie').json()['url']
    user.can_media = False
    monkeypatch.setattr(routes.settings_store, 'get', lambda *args: 'http://nas.lan:3051')
    monkeypatch.setattr(routes.lb_auth, 'shared_secret', lambda: 'test-secret')
    class NoNetwork:
        def build_request(self, *args, **kwargs):
            pytest.fail('revoked permission reached NAS')
    monkeypatch.setattr(routes, '_proxy_client', NoNetwork())
    assert client.get(url).status_code == 403
    assert client.get('/api/media-center').status_code == 403


def test_cookie_free_ticket_checks_persisted_permission(api):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import User
    client, docs, user, folder = api
    seed(docs, folder)
    user.nostr_npub, user.is_admin = VIEWER, False
    url = client.post('/api/media-center/abc/play/movie').json()['url']
    engine = create_engine('sqlite:///' + str(folder / 'users.db'))
    User.__table__.create(engine)
    with Session(engine) as db:
        account = User(username='viewer', password_hash='unused', nostr_npub=VIEWER, can_media=True)
        db.add(account)
        db.commit()
        client.app.dependency_overrides[routes.media_user_optional] = lambda: None
        client.app.dependency_overrides[routes.get_db] = lambda: db
        assert client.get(url).status_code == 200
        account.can_media = False
        db.commit()
        assert client.get(url).status_code == 403
    engine.dispose()


def test_media_permission_encrypted_roundtrip_and_cold_hydration(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import User
    from app.services import users_store, nostr_store
    from app.services.nostr import nip44
    key = bytes.fromhex('01' * 32)
    events = []
    async def publish(port, event, **kwargs):
        events.append(event)
        return True, ''
    monkeypatch.setattr(nostr_store, '_ws_publish', publish)
    monkeypatch.setattr(users_store._ss, '_operator_seckey', lambda db: key)
    monkeypatch.setattr(users_store._ss, '_port', lambda db: 7777)
    engine = create_engine('sqlite://')
    User.__table__.create(engine)
    with Session(engine) as db:
        account = User(username='viewer', password_hash='unused', nostr_npub=VIEWER, can_media=True)
        db.add(account)
        db.commit()
        assert asyncio.run(users_store.sync_user(db, account))
        assert 'can_media' not in events[-1]['content']
        record = json.loads(nip44.decrypt_self(key, events[-1]['content']))
        async def list_docs(*args, **kwargs):
            return {'user': record}
        monkeypatch.setattr(users_store.store, 'list_docs', list_docs)
        db.execute(User.__table__.delete())
        db.commit()
        db.expunge_all()
        assert asyncio.run(users_store.hydrate(db)) == 1
        restored = db.query(User).one()
        assert restored.can_media is True
        record['can_media'] = False
        assert asyncio.run(users_store.hydrate(db)) == 1
        assert restored.can_media is False
    engine.dispose()


def test_browser_auth_recovery_executes_real_helper():
    source = Path('static/js/client/app.js').read_text()
    helper = source[source.index('  async function _mediaCenterFetch('):source.index('  function clearMediaCenterArt(')]
    script = """
const assert = require('node:assert/strict');
let _aiToken='', _aiAuth=null, fail=false, statuses=[], calls=[], logins=0;
function _setAiToken(token){_aiToken=token;}
async function ensureAiSession(){
 if(fail)throw new Error('signer unavailable');
 if(!_aiToken){_aiToken='token'+(++logins);_aiAuth={};}
}
async function _streamFetch(url,opts){calls.push({url,opts,token:_aiToken});return {status:statuses.shift()};}
""" + helper + """
(async()=>{
 fail=true;
 await assert.rejects(_mediaCenterFetch('/api/media-center'),/signer unavailable/);
 assert.equal(calls.length,0);
 fail=false;statuses=[401,200];
 assert.equal((await _mediaCenterFetch('/api/media-center')).status,200);
 assert.equal(logins,2);assert.equal(calls.length,2);
 assert.notEqual(calls[0].token,calls[1].token);
 assert.equal(calls[0].opts.credentials,'include');
 statuses=[403];calls=[];
 assert.equal((await _mediaCenterFetch('/api/media-center')).status,403);
 assert.equal(calls.length,1);assert.equal(logins,2);
 statuses=[401,401];calls=[];
 assert.equal((await _mediaCenterFetch('/api/media-center')).status,401);
 assert.equal(calls.length,2); // never loop indefinitely
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_upgrade_adds_media_permission_default_denied(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from app import database
    engine = create_engine('sqlite:///' + str(tmp_path / 'upgrade.db'))
    routes.User.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE users DROP COLUMN can_media'))
        conn.execute(text("INSERT INTO users (username,password_hash,nostr_npub) VALUES ('old','unused',:key)"), {'key': VIEWER})
    monkeypatch.setattr(database, 'engine', engine)
    database._run_migrations()
    database._run_migrations()  # upgrades remain idempotent
    with engine.connect() as conn:
        assert conn.execute(text('SELECT can_media FROM users')).scalar_one() == 0
    engine.dispose()


def test_admin_can_inspect_allowed_roots_but_viewers_cannot(api):
    client, docs, user, folder = api
    config = client.get('/api/media-center/roots').json()
    assert config['roots'] == [{'path': str(folder), 'exists': True, 'readable': True}]
    assert config['host']
    user.is_admin = False
    assert client.get('/api/media-center/roots').status_code == 403


def test_folder_rejections_are_actionable_and_do_not_create_libraries(api):
    client, docs, user, folder = api
    cases = [('relative/path', 'absolute folder'), (str(folder / 'missing'), 'does not exist'),
             ('/etc', 'POSTERCHANAI_MEDIA_ROOTS')]
    regular = folder / 'not-a-directory'
    regular.write_text('test')
    cases.append((str(regular), 'not a file'))
    for path, expected in cases:
        response = client.post('/api/media-center', json={'name': 'Videos', 'folder': path})
        assert response.status_code == 400
        assert expected in response.json()['detail']
        assert 'index' not in docs
