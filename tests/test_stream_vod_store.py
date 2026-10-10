"""Saved stream recordings (`stream_vods`) as relay documents (#161) — RUN against the shipped relay.

Pins: the one-time copy's reader gives the rows the store serves; (token, started_at) stays unique, so a finalize
retried after an end-event re-delivery finds its own row; the listings keep their newest-first order and their 200
cap; and with the relay unreachable the listings answer 503 — never an empty list, which the client would stamp
onto the NIP-53 `recording` tag as "no replay".
"""
import asyncio
import itertools
import os
import pathlib
import socket
import sys

import pytest

from app.services import stream_vod_store
from app.services.relay_reader import Unavailable
from tests.blob_index_harness import reset_process_state, shared_relay, sql_session

_FIXTURES = (shared_relay,)
_seq = itertools.count(1)


def _copy(db):
    """What scripts/migrate_tables_to_nostr.py writes: the SQL reader's rows, as documents."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from scripts.nostr_migration import wave1_sources as W
    from app.services import doc_table_bulk
    rows = W.stream_vods(db)
    asyncio.run(doc_table_bulk.abulk_put(stream_vod_store.TABLE, rows))
    return rows


def _vod(i, *, user_id=1, token="tok", started_at=None, sha=None):
    return dict(id=i, user_id=user_id, pubkey="a" * 64, token=token, sha256=sha or "%064x" % i, mime="video/mp4",
                size=1000 + i, duration_s=60, title=None, started_at=started_at or 1700000000 + i,
                created_at=1700000100 + i)


@pytest.fixture
def vods(shared_relay, monkeypatch):  # noqa: F811
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(shared_relay.port))
    monkeypatch.setattr(stream_vod_store, "TABLE", "stream_vods_t%d_%d" % (os.getpid(), next(_seq)))
    reset_process_state()
    yield shared_relay
    reset_process_state()


def test_the_copied_rows_keep_their_order(vods):
    db = sql_session(vods=[_vod(1, started_at=10), _vod(2, started_at=30), _vod(3, started_at=20, token="other"),
                           _vod(4, user_id=2, started_at=40)])
    assert len(_copy(db)) == 4
    reset_process_state()
    assert [v["id"] for v in stream_vod_store.for_user(1)] == [2, 3, 1]
    assert [v["id"] for v in stream_vod_store.for_token("tok")] == [4, 2, 1]


def test_a_retried_finalize_finds_its_own_recording(vods):
    assert not asyncio.run(stream_vod_store.aexists("tok", 99))
    asyncio.run(stream_vod_store.aadd(user_id=1, pubkey="a" * 64, token="tok", sha256="f" * 64, mime="video/mp4",
                                      size=5, duration_s=None, title=None, started_at=99))
    assert asyncio.run(stream_vod_store.aexists("tok", 99))
    assert [v["sha256"] for v in stream_vod_store.for_token("tok")] == ["f" * 64]


def test_the_listing_is_capped_at_200_newest(vods):
    _copy(sql_session(vods=[_vod(i, started_at=i) for i in range(1, 231)]))
    got = stream_vod_store.for_token("tok")
    assert len(got) == 200 and got[0]["started_at"] == 230 and got[-1]["started_at"] == 31


def test_an_unreachable_relay_is_a_503_never_no_recordings(vods, monkeypatch):
    from app.routers import streams
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(dead))
    reset_process_state()
    r = streams.stream_vods_by_token("tok", db=None)
    assert r.status_code == 503, "an unreadable table was listed as 'no recordings'"
    with pytest.raises(Unavailable):
        asyncio.run(stream_vod_store.aexists("tok", 1))


def test_the_route_filters_recordings_whose_blob_is_gone(vods, monkeypatch):
    from app.routers import streams
    from app.services import blob_index
    _copy(sql_session(vods=[_vod(1), _vod(2)]))
    monkeypatch.setattr(blob_index, "get", lambda sha: object() if sha == "%064x" % 2 else None)
    r = streams.stream_vods_by_token("tok", db=None)
    assert [v["id"] for v in r["vods"]] == [2]
