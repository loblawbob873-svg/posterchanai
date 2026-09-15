"""Completed transport streams must not compete with new encoder jobs."""
import asyncio
from pathlib import Path

import pytest

from app.services import media_center as media


def test_cached_segment_bypasses_full_encoder_and_job_queue(tmp_path, monkeypatch):
    source = tmp_path / 'movie.mp4'
    source.write_bytes(b'source')
    stat = source.stat()
    library = {'folder': str(tmp_path), 'encoder': 'cpu'}
    item = {'path': source.name, 'id': 'movie', 'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns, 'duration': 13, 'video': True}
    monkeypatch.setenv('POSTERCHANAI_MEDIA_ROOTS', str(tmp_path))
    monkeypatch.setenv('POSTERCHANAI_MEDIA_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setattr(media, 'encoder_candidates', lambda *_: ['libx264'])
    def encode(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b'cached transport stream')
    monkeypatch.setattr(media.subprocess, 'run', encode)
    assert media.transcode(library, item, '360p', 0) == b'cached transport stream'
    monkeypatch.setattr(media.subprocess, 'run', lambda *_args, **_kwargs: pytest.fail('cache hit encoded again'))

    async def exercise():
        config = {**media.DEFAULT_LIMITS, 'max_transcodes': 1, 'max_streams': 1}
        monkeypatch.setattr(media, '_job_condition', asyncio.Condition())
        monkeypatch.setattr(media, '_active_transcodes', 0)
        queued = {str(i): object() for i in range(3)}
        monkeypatch.setattr(media, '_segment_jobs', queued)
        async with media.transcode_slot(config):
            assert await asyncio.wait_for(media.segment(library, item, '360p', 0, config), 2) == b'cached transport stream'
            assert media._active_transcodes == 1 and len(queued) == 3
            with pytest.raises(RuntimeError, match='queue is full'):
                await media.segment(library, item, '360p', 1, config)
            # Cached data must never bypass the existing source-change validation.
            source.write_bytes(b'changed source')
            with pytest.raises(ValueError, match='rescan'):
                await media.segment(library, item, '360p', 0, config)
        assert media._active_transcodes == 0
    asyncio.run(exercise())
