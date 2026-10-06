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


def test_a_killed_test_process_does_not_leave_its_postgres_running(tmp_path):
    """atexit never runs on SIGKILL, and the gate kills a shard that times out: three clusters from a gate
    24 hours earlier were found still running on server1. The watchdog must stop it and remove its dir."""
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path
    import pytest
    import tests.scratch_postgres as sp
    if not (sp._bin("initdb") and sp._bin("pg_ctl")):
        pytest.skip("no Postgres binaries on this machine")
    root = Path(__file__).resolve().parents[1]
    code = ("import os,signal,tests.scratch_postgres as sp\n"
            "p=sp.params(); print(p['host'], flush=True)\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n")
    env = dict(os.environ, PYTHONPATH=str(root))
    env.pop("PC_TEST_PG_PORT", None)
    r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=180)
    sock = r.stdout.strip()
    assert sock.startswith("/"), (r.stdout, r.stderr[-500:])
    cluster_root = Path(sock).parent
    deadline = time.time() + 30
    while time.time() < deadline and cluster_root.exists():
        time.sleep(1)
    assert not cluster_root.exists(), "the killed process's Postgres directory was left behind"
    alive = subprocess.run(["pgrep", "-f", f"postgres -D {cluster_root}"], capture_output=True, text=True).stdout
    assert not alive.strip(), f"its Postgres is still running: {alive}"
