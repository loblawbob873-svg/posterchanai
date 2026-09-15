"""Execute the shipped aggregation SQL over public relay-shaped records."""
from sqlalchemy import create_engine, text
from app.services import stats_service as stats

NOW = 1_800_001_237


class SQLiteRelay:
    """Only translate Postgres ANY syntax; all predicates/grouping execute unchanged."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, params=None):
        sql = str(statement)
        params = dict(params or {})
        if 'ANY(:kinds)' in sql:
            kinds = params.pop('kinds')
            sql = sql.replace('= ANY(:kinds)', 'IN (' + ','.join(map(str, kinds)) + ')')
        return self.connection.execute(text(sql), params)


def snapshot(extra=()):
    engine = create_engine('sqlite://')
    with engine.connect() as db:
        db.execute(text('CREATE TABLE events (id text PRIMARY KEY, pubkey text, created_at integer, kind integer, origin text)'))
        db.execute(text('CREATE TABLE event_tags (event_id text, tag text, value text)'))

        def add(id, age=1, kind=1, origin='direct', tags=(('t', 'monerotip'),)):
            db.execute(text('INSERT INTO events VALUES (:id, :pubkey, :created_at, :kind, :origin)'),
                       dict(id=id, pubkey='person', created_at=NOW-age, kind=kind, origin=origin))
            for tag, value in tags:
                db.execute(text('INSERT INTO event_tags VALUES (:id,:tag,:value)'),dict(id=id, tag=tag, value=value))

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
        return stats._series(SQLiteRelay(db), NOW)


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
