"""A Postgres for the tests that need one -- never a production database, never "skip if absent".

Seven test files reached `host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan` and SKIPPED
when nothing answered. On 2026-10-01 server1's PosterChan database moved to nas.lan and the local
Postgres was switched off, so ~20 relay tests (the prune rules that keep calendars, contacts and paid
posts alive, the quote index, git state) quietly stopped running -- caught only because
test_the_suite_can_actually_fail noticed a guard that no longer failed when broken.

So: use the local server when one answers (CI, a dev box -- unchanged), otherwise start a PRIVATE
throwaway cluster (initdb into a temp dir, trust auth, a free port, the same role/database names the
tests expect) once per test process, and remove it at exit. A machine with no Postgres binaries at
all falls back to the old address and the old skip, which test_the_suite_can_actually_fail reports.
"""
from __future__ import annotations

import atexit
import glob
import os
import shutil
import socket
import subprocess
import tempfile

ROLE, DB = "posterchan", "posterchan_relay"
_DEFAULT = {"host": "127.0.0.1", "port": 5432, "dbname": DB, "user": ROLE}
_cached: dict | None = None


def _answers(params: dict) -> bool:
    try:
        import psycopg2
        psycopg2.connect(connect_timeout=3, **params).close()
        return True
    except Exception:
        return False


def _bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for pattern in ("/usr/lib64/postgresql-*/bin/", "/usr/lib/postgresql/*/bin/", "/usr/local/pgsql/bin/"):
        for d in sorted(glob.glob(pattern), reverse=True):
            if os.access(os.path.join(d, name), os.X_OK):
                return os.path.join(d, name)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_private() -> dict | None:
    initdb, pg_ctl = _bin("initdb"), _bin("pg_ctl")
    if not (initdb and pg_ctl):
        return None
    root = tempfile.mkdtemp(prefix="pc-test-pg-")
    data, sock = os.path.join(root, "data"), os.path.join(root, "sock")
    os.makedirs(sock)
    env = dict(os.environ, LC_ALL="C")
    try:
        subprocess.run([initdb, "-D", data, "-U", ROLE, "--auth=trust", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True, env=env, timeout=120)
        # 5432 on purpose: the socket lives in our own directory and there is no TCP listener, so it
        # cannot clash -- and code that passes only a host (the bots' akkoma_db) finds it.
        port = 5432
        subprocess.run([pg_ctl, "-D", data, "-s", "-w", "-l", os.path.join(root, "log"),
                        "-o", f"-p {port} -k {sock} -c listen_addresses= -c fsync=off "
                              f"-c synchronous_commit=off -c full_page_writes=off -c max_connections=200",
                        "start"],
                       check=True, capture_output=True, env=env, timeout=120)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        return None

    def _stop():
        subprocess.run([pg_ctl, "-D", data, "-s", "-m", "immediate", "stop"], capture_output=True, timeout=60)
        shutil.rmtree(root, ignore_errors=True)
    atexit.register(_stop)
    # The private SOCKET, not TCP: on server1 a connection to 127.0.0.1 arrives as 192.168.0.2 and the
    # trust rules initdb writes do not match it. The socket lives in our own temp dir.
    params = {"host": sock, "port": port, "dbname": "postgres", "user": ROLE}
    import psycopg2
    conn = psycopg2.connect(**params)
    conn.autocommit = True
    conn.cursor().execute(f'CREATE DATABASE "{DB}" OWNER "{ROLE}"')
    conn.close()
    return {**params, "dbname": DB}


def params() -> dict:
    """libpq keyword parameters for a Postgres the tests may use freely."""
    global _cached
    if _cached is None:
        if os.environ.get("PC_TEST_PG_PORT"):
            _cached = {**_DEFAULT, "port": int(os.environ["PC_TEST_PG_PORT"])}
        elif _answers(_DEFAULT):
            _cached = dict(_DEFAULT)
        else:
            _cached = _start_private() or dict(_DEFAULT)
    return dict(_cached)


def dsn() -> str:
    """The same, as a libpq connection string."""
    return " ".join(f"{k}={v}" for k, v in params().items())
