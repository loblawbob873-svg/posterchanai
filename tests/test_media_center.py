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

from app.auth import get_admin_user, get_current_user
from app.routers import media_center as routes
from app.services import media_center as media

OWNER = "11" * 32
VIEWER = "22" * 32


@pytest.fixture
def api(monkeypatch, tmp_path):
    documents = {}
    user = SimpleNamespace(nostr_npub=OWNER, is_admin=True)
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
    media._sessions.clear()
    media._catalog_cache.clear()
    media._rate_due.clear()
    media._failed_encoders.clear()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_admin_user] = lambda: user
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
