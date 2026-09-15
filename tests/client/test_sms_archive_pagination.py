"""Actual Texts archive reader against the bundled relay's 5,000-row page ceiling."""
from tests.client.test_sms_archive import run, ev


def pages(result):
    return [c[1] for c in result['calls'] if c[0] == 'archivePage']


def test_fresh_desktop_reads_more_than_one_relay_cap():
    result = run(isPhone=False, generatedArchive=6200, steps=['load', 'settleArchive'])
    assert len(result['docs']) == 6200
    assert len(pages(result)) > 2
    assert not result['historyIncomplete']


def test_same_second_larger_than_relay_cap_uses_stable_id_boundary():
    result = run(isPhone=False, generatedArchive=6200, sameSecondArchive=True,
                 steps=['load', 'settleArchive'])
    assert len(result['docs']) == 6200
    assert any('_cursor' in p for p in pages(result))
    assert not result['historyIncomplete']


def test_unlabelled_archive_pages_without_opening_other_private_documents():
    result = run(isPhone=False, generatedArchive=6200, unlabelledArchive=True,
                 sameSecondArchive=True, generatedOtherArchive=5500, steps=['load', 'settleArchive'])
    assert len(result['docs']) == 6200
    assert any('_cursor' in p and '#l' not in p for p in pages(result))


def test_partial_page_keeps_cache_and_retries_instead_of_latching_complete():
    cached = ev('pcai:sms:cached', {'address':'+15550100','body':'saved','date':1})
    result = run(isPhone=False, cached=[cached], generatedArchive=6200,
                 failArchiveAt=1, steps=['load', 'settle'])
    assert 'pcai:sms:cached' in result['docs']
    assert result['historyIncomplete']
    recovered = run(isPhone=False, cached=[cached], generatedArchive=6200,
                    failArchiveAt=1, steps=['load', 'recoverArchive', 'settleArchive'])
    assert len(recovered['docs']) == 6201
    assert not recovered['historyIncomplete']


def test_unsupported_same_second_cursor_stops_without_claiming_full_history():
    result = run(isPhone=False, generatedArchive=6200, sameSecondArchive=True,
                 ignoreArchiveCursor=True, stopIncomplete=True, steps=['load', 'settleArchive'])
    assert len(result['docs']) == 5000
    assert result['historyIncomplete']
    assert len(pages(result)) < 30


def test_account_switch_during_query_discards_late_page_and_stops_reads():
    result = run(isPhone=False, generatedArchive=6200, switchArchiveAt=2, steps=['load'])
    assert len(result['docs']) == 256
    assert len(pages(result)) == 2


def test_incomplete_unlabelled_sweep_is_retried_on_next_open():
    result = run(isPhone=False, generatedArchive=6200, unlabelledArchive=True,
                 failArchiveAt=2, steps=['load', 'settle', 'render', 'settleArchive'])
    assert len(result['docs']) == 6200
    assert not result['historyIncomplete']
    assert sum('#l' not in p and 'until' not in p for p in pages(result)) >= 2
    assert not result['published']


def test_bundled_relay_cursor_contract_reaches_the_remainder_of_a_full_second():
    """Exercise production SQL, independently of the JS relay fixture's ordering assumptions."""
    import json
    import sqlite3
    # Compile the exact production read methods without importing its PostgreSQL runtime.
    # The release gate intentionally has no database driver/server; this test owns SQLite only.
    import ast
    import time
    from pathlib import Path
    source=Path(__file__).resolve().parents[2]/'app/services/nostr_relay/store.py'
    klass=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='RelayStore')
    klass.body=[n for n in klass.body if isinstance(n,ast.FunctionDef) and n.name in {'_query_sync','_query_one','_build_where'}]
    namespace={'json':json,'time':time,'_PgConn':sqlite3.Connection}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[klass],type_ignores=[])),str(source),'exec'),namespace)
    RelayStore=namespace['RelayStore']
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''CREATE TABLE events(id TEXT PRIMARY KEY, pubkey TEXT,
        created_at INTEGER, kind INTEGER, expiration INTEGER, raw TEXT);''')
    rows = [{'id':format(i, '064x'), 'pubkey':'me', 'kind':30078, 'created_at':100000}
            for i in range(6200)]
    conn.executemany('INSERT INTO events VALUES (?, ?, ?, ?, NULL, ?)',
                     [(e['id'], e['pubkey'], e['created_at'], e['kind'], json.dumps(e))
                      for e in rows])
    store = RelayStore.__new__(RelayStore)
    store._conn = lambda: conn
    try:
        f = {'authors':['me'], 'kinds':[30078], 'limit':20000}
        first = store._query_sync([f], 5000)
        assert len(first) == 5000
        f['_cursor'] = [first[-1]['created_at'], first[-1]['id']]
        rest = store._query_sync([f], 5000)
        assert len(rest) == 1200
        assert len({e['id'] for e in first + rest}) == 6200
    finally:
        conn.close()


def test_successful_label_read_cannot_hide_a_failed_legacy_sweep():
    result = run(isPhone=False, generatedArchive=6200, unlabelledArchive=True,
                 failArchiveAt=2, holdArchiveAfterFailure=True,
                 steps=['load', 'settle', 'recoverArchive', 'settle'])
    assert result['historyIncomplete']


def test_sparse_old_relay_cannot_skip_dense_relay_middle():
    result=run(isPhone=False,generatedArchive=6200,multiRelayArchive=True,
               steps=['load','settleArchive'])
    assert len(result['docs'])==6200
    assert not result['historyIncomplete']


def test_mixed_relay_caps_preserve_dense_middle_and_full_same_second():
    for same_second in [False,True]:
        result=run(isPhone=False,generatedArchive=6200,multiRelayArchive=True,
                   mixedArchiveCaps=True,sameSecondArchive=same_second,
                   steps=['load','settleArchive'])
        assert len(result['docs'])==6200
        assert not result['historyIncomplete']


def test_lower_relay_cap_does_not_prove_exhaustion():
    result=run(isPhone=False,generatedArchive=6200,archiveRelayCap=100,
               sameSecondArchive=True,steps=['load','settleArchive'])
    assert len(result['docs'])==6200
    assert not result['historyIncomplete']


def test_one_old_row_on_second_relay_does_not_turn_pages_into_one_request_per_message():
    result=run(isPhone=False,generatedArchive=6200,multiRelayArchive=True,sparseArchiveRelay=True,
               steps=['load','settleArchive'])
    assert len(result['docs'])==6200
    assert not result['historyIncomplete']
    assert len(pages(result))<100
