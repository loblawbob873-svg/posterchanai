"""A claimed recording becomes a VOD on the streamer's drive -- the finalizer itself, with no MediaMTX.

stream_vod_service._process looked the streamer up with `user_by_token(...)` WITHOUT await after that lookup became
async (#161 wave 2): the coroutine was used as the user id, `user_table.aget` raised, the job was logged "finalize
failed ... (will retry)" every pass and dropped after a day -- not one recording was saved. The end-to-end proof is
tests/client/test_golive_real_mediamtx.py (a real MediaMTX recording a real Go Live); this is the same job on a
staged segment, so it runs where that one cannot (no MediaMTX, no ffmpeg needed: compression failing falls back
to the original segment, which is the designed behaviour).
"""
import asyncio
import os
import time

from app.services import settings_store, stream_service, stream_vod_service, stream_vod_store, user_settings_table
from tests.doc_table_mem import mem_tables
from tests.user_fixtures import seed_user

TOKEN = 'abcdef0123456789'


def test_a_claimed_session_is_saved_as_a_vod_for_its_streamer(monkeypatch, tmp_path):
    mem = mem_tables(monkeypatch)
    seed_user(mem, 7, username='streamer', nostr_npub='%064x' % 7, can_stream=True, stream_record=True)
    rec, blobs = tmp_path / 'rec', tmp_path / 'blobs'
    for k, v in {'stream_record_enabled': 'true', 'stream_record_dir': str(rec), 'blossom_enabled': 'true',
                 'blossom_storage_backend': 'local', 'blossom_storage_path': str(blobs)}.items():
        monkeypatch.setitem(settings_store._CACHE, k, v)
    from app.services import blossom_service
    monkeypatch.setattr(blossom_service, '_DEFAULT_BLOB_DIR', str(blobs))
    monkeypatch.setenv('VIDEO_HWACCEL', '0')
    assert stream_service.recording_dir() == str(rec)

    async def go():
        await user_settings_table.aset(None, 7, 'stream_token', TOKEN)
        staging = rec / '.jobs' / ('%s__1700000000__1_0' % TOKEN)
        staging.mkdir(parents=True)
        (staging / '2026-10-23_10-00-00-000000.mp4').write_bytes(b'\x00\x00\x00\x18ftypmp42' + os.urandom(4096))
        await stream_vod_service._process(str(staging))
        return staging
    staging = asyncio.run(go())
    rows = asyncio.run(_rows())
    assert len(rows) == 1, ('no VOD was indexed for the streamer', rows)
    v = rows[0]
    assert v['user_id'] == 7 and v['token'] == TOKEN and v['started_at'] == 1700000000, v
    assert (blobs / v['sha256'][:2] / v['sha256']).is_file(), 'the recording bytes are not on the drive'
    assert not staging.exists(), 'a saved job was left staged (it would be re-processed every pass)'


async def _rows():
    t = await stream_vod_store._require()
    return [r for _k, r in await t.awhere(lambda r: r.get('token') == TOKEN)]


def test_a_reaper_pass_at_go_live_does_not_throw_the_recording_away(monkeypatch, tmp_path):
    """The go-live marker is written at publish-auth, before MediaMTX has recorded anything and before a WebRTC path
    is even "ready". A reaper pass in that window used to read "no footage, not publishing" as a stream that never
    went live and delete the marker -- after which the recording is an orphan and is never saved."""
    rec = tmp_path / 'rec'
    monkeypatch.setitem(settings_store._CACHE, 'stream_record_dir', str(rec))
    from app.services import stream_end_service

    async def not_yet(token):
        return False
    monkeypatch.setattr(stream_end_service, 'is_publishing', not_yet)
    stream_vod_service.mark_golive(TOKEN)
    marker = rec / TOKEN / '.golive'
    assert marker.is_file()
    asyncio.run(stream_vod_service._claim_ended_sessions())
    assert marker.is_file(), 'the marker of a stream that had just gone live was deleted'
    # ...while a marker that really produced nothing, long enough ago, is still cleared (the designed behaviour).
    old = time.time() - stream_vod_service._END_QUIET_S - 5
    marker.write_text(str(int(old)))
    asyncio.run(stream_vod_service._claim_ended_sessions())
    assert not marker.exists(), 'a stale marker with no footage was kept'
