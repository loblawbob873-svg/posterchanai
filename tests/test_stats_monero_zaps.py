"""Monero tip counts, through the shipped relay aggregates (both backends) and stats_service's bucketing.

The relay counts (nostr_relay/aggregates.py) -- on Postgres SQL (run here on sqlite) or on PosterChanDB -- and
stats_service._series aligns its rows to the page's buckets. Every snapshot is checked on BOTH backends.
"""
import tempfile

import pytest

from app.services import stats_service as stats
from app.services.nostr_relay import aggregates
from tests.relay_agg_fixture import ev, pcdb_source, sql_source

NOW = 1_800_001_237
BACKEND = ["sql"]


@pytest.fixture(autouse=True, params=["sql", "pcdb"])
def _backend(request):
    BACKEND[0] = request.param
    yield


def snapshot(extra=()):
    rows = []

    def add(id, age=1, kind=1, origin='direct', tags=(('t', 'monerotip'),)):
        rows.append(ev('person', kind, NOW - age, tags=tags, origin=origin))

    add('post-tip', tags=(('t','monerotip'),('t','monerotip'),('e','post'),('amount_xmr','0.0002')))
    add('profile-tip-no-amount')
    add('yesterday-tip', age=25*3600)
    add('previous-hour-tip', age=2*3600)
    add('last-month', age=31*86400)
    add('future', age=-3600)
    add('ordinary-monero-discussion', tags=(('t','monero'),))
    add('amount-without-tip', tags=(('amount_xmr','10'),))
    add('bch-tip', tags=(('t','bchtip'),))
    add('wrong-kind', kind=7)
    add('lightning', kind=9735, tags=())
    for origin in ('wot','ancestor','bridge'):
        add(origin, origin=origin)
    for event in extra:
        add(**event)
    src = sql_source(rows) if BACKEND[0] == "sql" else pcdb_source(rows, tempfile.mkdtemp(prefix="pcagg-"), NOW)
    return stats._series(aggregates.server_stats(src, NOW), NOW)


def test_local_monero_tips_have_separate_range_counts():
    windows = snapshot()
    # Profile tips and notes without a disclosed amount still count. Duplicate
    # hashtags, other coins, synced notes, future notes and wrong kinds do not.
    assert {k:sum(w['series']['monero_zaps']) for k,w in windows.items()} == {'minute':2,'hour':3,'day':4}
    assert all(sum(w['series']['zaps']) == 1 for w in windows.values())


def test_current_partial_bucket_is_visible_and_series_remains_bounded():
    windows = snapshot()
    for key, _, step in stats.WINDOWS:
        w = windows[key]
        assert len(w['series']['monero_zaps']) == w['n']
        assert w['t0'] + (w['n']-1)*step <= NOW < w['t0'] + w['n']*step
        assert w['series']['monero_zaps'][-1] == (3 if key == 'day' else 2)
        assert w['series']['zaps'][-1] == 1


def test_tip_breakdown_does_not_duplicate_events_or_remove_notes():
    window = snapshot()['minute']
    assert sum(window['series']['notes']) == 5
    assert window['totals']['events'] == 7
    assert window['totals']['people'] == 1


def test_exact_rolling_edges_keep_oldest_and_current_activity():
    for key, span, step in stats.WINDOWS:
        baseline = snapshot()[key]
        window = snapshot((
            dict(id='oldest-included', age=span),
            dict(id='oldest-excluded', age=span+1),
            dict(id='upper-exclusive', age=0),
        ))[key]
        assert sum(window['series']['monero_zaps']) == sum(baseline['series']['monero_zaps']) + 1
        assert window['series']['monero_zaps'][0] == baseline['series']['monero_zaps'][0] + 1
        assert window['totals']['events'] == baseline['totals']['events'] + 1
        assert window['n'] == span // step + 1
