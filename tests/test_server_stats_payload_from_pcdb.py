"""DISCOVER -> SERVER STATS IS BUILT FROM A RELAY WHOSE ONLY STORE IS POSTERCHANDB -- the whole path the page uses.

Reported after deploy 216: Server Stats showed nothing. Both nodes run PosterChanDB alone since 2026-10-10 and
aggregates.source() fell through to `store._conn()` (Postgres), so every ask answered Unavailable (fix f60d79c09).
tests/test_relay_stats_count_from_the_store.py proves `aggregates.compute()` on a PcdbRelayStore. This goes the rest
of the way, the way the page gets its numbers: the relay process's ask poller (`aggregates.ask_poller`, the
file-drop protocol the app and the relay share) answering `stats_service._compute()` -- the payload
`GET /client/stats` returns -- and asserts what the page would draw: the relay is NOT reported unavailable, and
today's posts and people are counted.

Fails on the pre-fix aggregates.py / pcdb_store.py with `relay_unavailable: True` and every count None.
"""
import asyncio
import functools
import threading
import time

from app.services import stats_service
from app.services.nostr_relay import aggregates as A
from tests.relay_agg_fixture import ev
from tests.relay_backends import pcdb_factory


def test_the_stats_page_payload_counts_from_a_posterchandb_relay(tmp_path, monkeypatch):
    A._CACHE.clear()
    now = int(time.time())
    loop = asyncio.new_event_loop()
    ready, stop = threading.Event(), {}

    def relay_thread():
        asyncio.set_event_loop(loop)

        async def main():
            stop['ev'] = asyncio.Event()
            ready.set()
            await A.ask_poller(st, str(tmp_path / 'control'), stop['ev'], interval=0.05)
        loop.run_until_complete(main())

    with pcdb_factory() as make:
        st = make(loop)
        evs = [ev('alice', 1, now - 60), ev('bob', 1, now - 120), ev('carol', 1, now - 3600), ev('bob', 7, now - 200)]

        async def seed():
            for e, origin in evs:
                assert await st.add_event(e, origin=origin)
        loop.run_until_complete(seed())
        t = threading.Thread(target=relay_thread, daemon=True)
        t.start()
        assert ready.wait(10)
        try:
            # The app's side of the protocol, pointed at this relay's control directory.
            monkeypatch.setattr(A, 'ask', functools.partial(A.ask, control_dir=str(tmp_path / 'control'),
                                                            check_running=False))
            monkeypatch.setattr(stats_service, '_counter_series', lambda now: {'days': [], 'metrics': {}})
            monkeypatch.setattr(stats_service, '_relay', lambda now, origins: {'origins': origins})
            data = stats_service._compute()
        finally:
            loop.call_soon_threadsafe(stop['ev'].set)
            t.join(10)
        assert data['relay_unavailable'] is False, data
        assert data['relay_backend'] == 'posterchandb', data['relay_backend']
        assert data['totals']['notes'] and data['totals']['notes'] >= 3, data['totals']
        series = ((data['windows'].get('day') or {}).get('series') or {})
        assert sum(series.get('notes') or []) >= 3, ('no posts drawn in the 30-day chart', sorted(series))
        people = (data['windows']['day'].get('totals') or {}).get('people')
        assert people and people >= 3, ('nobody counted as active this month', data['windows']['day'].get('totals'))
    loop.close()
