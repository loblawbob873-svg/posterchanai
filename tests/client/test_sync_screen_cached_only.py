"""Opening Folder Sync must never turn a cache read into a full network/decrypt pass."""

from pathlib import Path


SYNC = (Path(__file__).resolve().parents[2] / "static/js/client/sync.js").read_text()


def test_screen_count_reads_only_the_cached_record_set():
    start = SYNC.index("function _countsAsk(f)")
    body = SYNC[start:SYNC.index("\n  }", start) + 4]
    assert "stateS.cached(key)" in body
    assert "stateS.load(key)" not in body


def test_cached_read_cannot_reanchor_or_call_the_server():
    start = SYNC.index("async cached(key)")
    body = SYNC[start:SYNC.index("\n    },", start)]
    assert "this._cache(key)" in body
    assert "_statePost" not in body
    assert ".load(" not in body
