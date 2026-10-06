"""The tests' Postgres is never a server that merely answers on this machine.

`tests/scratch_postgres.py` used 127.0.0.1:5432/posterchan_relay whenever something answered there -- the
PRODUCTION database's name on nas.lan, and server1's own production Postgres until 2026-10-01. A
`pcai_prune_test_…` schema was found inside a production database on 2026-10-05. Now: a private
throwaway cluster, or an address nothing answers on (the tests skip), or a server named explicitly.
"""
import psycopg2


def _fresh(monkeypatch):
    import tests.scratch_postgres as sp
    monkeypatch.setattr(sp, "_cached", None)          # restored after the test, so no later test sees a fake
    monkeypatch.delenv("PC_TEST_PG_PORT", raising=False)
    tried = []
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: tried.append(k) or (_ for _ in ()).throw(AssertionError(k)))
    return sp, tried


def test_a_server_answering_on_the_default_address_is_never_used(monkeypatch):
    sp, tried = _fresh(monkeypatch)
    monkeypatch.setattr(sp, "_start_private", lambda: {"host": "/tmp/private-sock", "port": 5432,
                                                        "dbname": sp.DB, "user": sp.ROLE})
    assert sp.params()["host"] == "/tmp/private-sock"
    assert tried == [], ("the harness probed a server it did not start", tried)


def test_no_postgres_binaries_means_an_address_nothing_answers_on(monkeypatch):
    sp, tried = _fresh(monkeypatch)
    monkeypatch.setattr(sp, "_start_private", lambda: None)
    p = sp.params()
    assert p["host"] != "127.0.0.1" and p["host"].startswith("/nonexistent"), p
    assert tried == []


def test_a_named_test_server_is_still_honoured(monkeypatch):
    sp, _ = _fresh(monkeypatch)
    monkeypatch.setenv("PC_TEST_PG_PORT", "6543")
    assert sp.params()["port"] == 6543
