"""PosterChanDB beside the relay's Postgres (posterchandb/mirror.py), on the relay's REAL store code.

What has to hold before the relay can answer from RAM, each checked against Postgres itself:

  * a mirror started over a populated database fills itself in the background WHILE the relay keeps storing,
    purging and pruning -- and ends up answering every random filter exactly as Postgres does;
  * the relay's own `_query_sync` then serves from the mirror (counted), and the answers are still Postgres's;
  * a clean close leaves CLEAN and the next start reopens without reloading; a crash (no close) reloads;
  * the relay running with the mirror OFF clears CLEAN, so turning it back on reloads rather than serving
    a directory that missed those writes;
  * a mirror that cannot be trusted -- its queue overflowed, a query threw -- never answers: the query
    goes to Postgres and gets Postgres's answer.
"""
import asyncio
import shutil
import subprocess
import os
import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.services.nostr_relay import store as relay          # noqa: E402
from app.services.posterchandb import mirror as M             # noqa: E402
from tests import scratch_postgres                             # noqa: E402
from tests.test_posterchandb_vs_relay import Gen, ORIGINS      # noqa: E402

DSN = scratch_postgres.dsn()


def _admin():
    try:
        c = psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:      # noqa: BLE001
        pytest.skip("Postgres not reachable: %s" % e)
    c.autocommit = True
    return c


@pytest.fixture
def relay_pg():
    schema = "pcai_pcdb_mirror_" + uuid.uuid4().hex[:10]
    c = _admin()
    c.cursor().execute(f'CREATE SCHEMA "{schema}"')
    c.close()
    dsn = DSN + f" options=-csearch_path={schema}"
    loop = asyncio.new_event_loop()
    rs = relay.RelayStore(dsn)
    rs.open(loop)
    try:
        yield rs, dsn
    finally:
        if rs.mirror is not None:
            rs.mirror.close()
        rs.close()
        loop.close()
        c = _admin()
        c.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        c.close()


def _mirror(path, dsn, mode="serve", **kw):
    kw.setdefault("maintenance", False)
    kw.setdefault("sample", 1.0)
    return M.Mirror(path, mode, lambda: psycopg2.connect(dsn, connect_timeout=5), flush_interval=3600, **kw)


def W(rs, fn, *a):
    """Run a write on the relay's WRITER thread, as every write to `events` is in production (RelayStore._w) --
    the property the mirror's exact cut-over rests on."""
    return rs._write_exec.submit(fn, *a).result()


def _attach(rs, m):
    rs.attach_mirror(m)
    return m


def _stop(rs):
    """A clean relay stop, as thread.py does it: detach on the writer thread, then close with the token."""
    m, token = rs.detach_mirror()
    m.close(clean_token=token)
    return m


def _until(cond, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _pg_answer(rs, flt):
    return [e["id"] for e in rs._query_one(rs._conn(), dict(flt))]


def _feed(rs, gen, n):
    for _ in range(n):
        W(rs, rs._add_event_sync, gen.event(), gen.r.choice(ORIGINS))


def _same_answers(rs, gen, n=300):
    """The relay's own query path (served by the mirror when it can) against Postgres directly."""
    bad = []
    for _ in range(n):
        f = gen.flt()
        want = _pg_answer(rs, f)
        got = [e["id"] for e in rs._query_sync([f], 5000)]
        if want != got:
            made = {e["id"]: e for e in gen.made}
            odd = [x for x in got if x not in want][:2] + [x for x in want if x not in got][:2]
            bad.append({"keys": sorted(f), "postgres": len(want), "relay": len(got),
                        "differ": [{"in": "relay" if x in got else "postgres", "kind": made[x]["kind"],
                                    "age": int(time.time()) - made[x]["created_at"],
                                    "tags": [t[0] for t in made[x]["tags"]], "pg_has": rs._has_sync(x)}
                                   for x in odd if x in made]})
    return bad


@pytest.mark.parametrize("seed", [31, 32])
def test_a_mirror_filled_while_the_relay_writes_answers_exactly_like_postgres(relay_pg, tmp_path, seed):
    rs, dsn = relay_pg
    gen = Gen(seed)
    _feed(rs, gen, 900)                                     # history the load must bring over
    m = _attach(rs, _mirror(str(tmp_path / "relay"), dsn))
    # the relay keeps working while the mirror copies: new events, a purge, a prune, re-sent old events
    _feed(rs, gen, 200)
    W(rs, rs._delete_pubkeys_sync, gen.authors[:1], False)
    rs.retention_days = 1
    W(rs, rs._prune_sync, 0)
    for ev in list(gen.made)[:150]:
        W(rs, rs._add_event_sync, dict(ev), gen.r.choice(ORIGINS))
    assert _until(lambda: m.state in ("ready", "stale", "failed")), m.stats()
    assert m.state == "ready", m.stats()
    _feed(rs, gen, 200)                                     # and after it is ready
    assert not _same_answers(rs, gen), "the relay answered differently from Postgres"
    assert m.counters["served"] > 0, "nothing was answered from RAM: the test proved nothing"
    assert m.counters["fallback"] == 0, m.stats()


def test_a_clean_close_reopens_without_reloading_and_a_crash_reloads(relay_pg, tmp_path):
    rs, dsn = relay_pg
    gen = Gen(33)
    _feed(rs, gen, 600)
    path = str(tmp_path / "relay")
    m = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m.state == "ready"), m.stats()
    assert m.counters["copied"] > 0
    _stop(rs)
    assert os.path.exists(os.path.join(path, M.CLEAN))

    m2 = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m2.state == "ready"), m2.stats()
    assert m2.counters["copied"] == 0, "a clean reopen copied Postgres again"
    assert not os.path.exists(os.path.join(path, M.CLEAN)), "CLEAN must go while the store is open"
    _feed(rs, gen, 100)
    assert not _same_answers(rs, gen, 200)
    rs.mirror = None
    m2._stop.set()                                          # a crash: no close, no CLEAN
    m2._thread.join(5)
    m2.store._file.close()

    m3 = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m3.state == "ready"), m3.stats()
    assert m3.counters["copied"] > 0, "an unclean directory was trusted instead of copied again"
    assert not _same_answers(rs, gen, 200)


def test_running_with_the_mirror_off_forces_a_reload_next_time(relay_pg, tmp_path, monkeypatch):
    rs, dsn = relay_pg
    monkeypatch.setenv("POSTERCHANDB_DIR", str(tmp_path))      # _start_mirror opens <data_dir>/relay
    gen = Gen(34)
    _feed(rs, gen, 300)
    path = str(tmp_path / "relay")
    m = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m.state == "ready")
    _stop(rs)
    from app.services.nostr_relay import thread
    thread._start_mirror(rs, {"pcdb_mode": "off", "pg_dsn": dsn})   # the relay starts with the mirror off …
    _feed(rs, gen, 100)                                                # … and writes things the mirror never saw
    assert not os.path.exists(os.path.join(path, M.CLEAN))
    m2 = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m2.state == "ready"), m2.stats()
    assert m2.counters["copied"] > 0
    assert not _same_answers(rs, gen, 200)


def test_an_untrusted_mirror_never_answers(relay_pg, tmp_path, monkeypatch):
    rs, dsn = relay_pg
    gen = Gen(35)
    _feed(rs, gen, 400)
    m = _attach(rs, _mirror(str(tmp_path / "relay"), dsn))
    assert _until(lambda: m.state == "ready")

    # a query that throws inside the mirror is answered by Postgres
    real = m.store.query
    monkeypatch.setattr(m.store, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    m.sample = 0.0
    before = m.counters["fallback"]
    assert not _same_answers(rs, gen, 50)
    assert m.counters["fallback"] > before
    monkeypatch.setattr(m.store, "query", real)

    # a write the mirror could not take: it stops answering for good (until a restart reloads it)
    monkeypatch.setattr(m._q, "put_nowait", lambda item: (_ for _ in ()).throw(M.queue.Full()))
    W(rs, rs._add_event_sync, gen.event(), "wot")
    assert m.state == "stale" and not m.serving()
    served = m.counters["served"]
    assert not _same_answers(rs, gen, 50)
    assert m.counters["served"] == served, "a stale mirror answered"


def test_shadow_mode_compares_but_never_answers(relay_pg, tmp_path):
    rs, dsn = relay_pg
    gen = Gen(36)
    _feed(rs, gen, 400)
    m = _attach(rs, _mirror(str(tmp_path / "relay"), dsn, mode="shadow"))
    assert _until(lambda: m.state == "ready")
    assert not _same_answers(rs, gen, 200)
    assert m.counters["served"] == 0
    assert m.counters["shadow_ok"] > 0 and m.counters["shadow_diff"] == 0, m.stats()


# ---------------------------------------------------------------- btrfs snapshots and restored backups
# nas.lan's cron.sh snapshots nodes every 6 hours (`btrfs sub snap /`, rsync to /raid) and pg_dumps the databases.
# A snapshot is an instant copy of the directory; restoring one later must never be served as if it were current.
def _snapshot(src, dst):
    """What `btrfs sub snap` gives a restore: the directory as it was at that instant (cp -a is the same bytes)."""
    subprocess.run(["cp", "-a", "--reflink=auto", src, dst], check=True)


def _restore(snap, path):
    shutil.rmtree(path)
    subprocess.run(["cp", "-a", "--reflink=auto", snap, path], check=True)


def test_restoring_an_older_clean_snapshot_copies_instead_of_serving_stale_data(relay_pg, tmp_path):
    rs, dsn = relay_pg
    gen = Gen(41)
    _feed(rs, gen, 400)
    path, old = str(tmp_path / "relay"), str(tmp_path / "snap-6h-ago")
    m = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m.state == "ready")
    _stop(rs)                                         # relay stopped cleanly: CLEAN present ...
    _snapshot(path, old)                              # ... and the 6-hourly snapshot catches it like that
    m2 = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m2.state == "ready") and m2.counters["copied"] == 0
    _feed(rs, gen, 300)                               # life goes on: new posts, a purge, a prune
    W(rs, rs._delete_pubkeys_sync, gen.authors[:1], False)
    _stop(rs)
    _restore(old, path)                               # somebody restores the older snapshot
    m3 = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m3.state in ("ready", "stale", "failed")), m3.stats()
    assert m3.counters["copied"] > 0, "an older snapshot with a CLEAN marker was trusted"
    assert m3.state == "ready" and not _same_answers(rs, gen, 200)


def test_a_snapshot_taken_while_running_is_never_trusted(relay_pg, tmp_path):
    rs, dsn = relay_pg
    gen = Gen(42)
    _feed(rs, gen, 400)
    path, live = str(tmp_path / "relay"), str(tmp_path / "snap-live")
    m = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m.state == "ready")
    _feed(rs, gen, 100)
    _snapshot(path, live)                             # mid-run: crash-consistent, no CLEAN
    _stop(rs)
    _restore(live, path)
    m2 = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m2.state == "ready"), m2.stats()
    assert m2.counters["copied"] > 0
    assert not _same_answers(rs, gen, 200)


def test_a_restored_database_makes_the_mirror_copy_again(relay_pg, tmp_path):
    rs, dsn = relay_pg
    gen = Gen(43)
    _feed(rs, gen, 400)
    path = str(tmp_path / "relay")
    m = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m.state == "ready")
    _stop(rs)
    # a pg_restore of an earlier dump: different rows, and relay_kv holds whatever token that dump had
    W(rs, rs._delete_pubkeys_sync, gen.authors[:2], False)
    W(rs, rs._kv_set_sync, relay.MIRROR_TOKEN_KEY, "clean:from-an-older-dump")
    m2 = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m2.state == "ready"), m2.stats()
    assert m2.counters["copied"] > 0, "the directory was trusted against a database it never matched"
    assert not _same_answers(rs, gen, 200)


def test_a_close_without_the_postgres_token_leaves_no_clean_marker(relay_pg, tmp_path):
    rs, dsn = relay_pg
    gen = Gen(44)
    _feed(rs, gen, 200)
    path = str(tmp_path / "relay")
    m = _attach(rs, _mirror(path, dsn))
    assert _until(lambda: m.state == "ready")
    rs.mirror = None
    m.close()                                         # no detach: nothing proves which Postgres state it matches
    assert not os.path.exists(os.path.join(path, M.CLEAN))
