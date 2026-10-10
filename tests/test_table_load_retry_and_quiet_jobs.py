"""A table whose load failed is RETRIED until it loads, and a scheduler job meeting a loading table says so in one
line -- never a traceback every 30 seconds (production 2026-10-10, after a restart).

  * a whole-table read whose inline load fails starts the background loader, which retries with backoff -- before,
    nothing retried it, and a caller that never asked again (a scheduler, the worker) kept an unloaded table;
  * the loader thread survives a CancelledError (a BaseException: a relay read cancelled by its timeout used to
    end the thread, and with it every retry);
  * scheduled_posts' poll pass, the bot manager's seed check and the instance-application retries skip quietly.
"""
import asyncio
import logging
import time

from app.services.doc_table import DocTable, Loading
from tests.doc_table_mem import mem_tables


def _wait(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_a_failed_load_is_retried_until_the_table_loads(monkeypatch):
    mem = mem_tables(monkeypatch)
    mem.put("reminders", "1", {"id": 1, "text": "hi"})
    mem.down = True
    t = DocTable("reminders")
    try:
        asyncio.run(t.aview())
    except Exception:
        pass
    else:
        raise AssertionError("an unreadable table answered")
    mem.down = False                                    # the relay recovers; nobody asks again
    assert _wait(lambda: t.loaded), "the failed load was never retried"
    assert t.peek("1")["text"] == "hi"


def test_the_loader_thread_survives_a_cancelled_read(monkeypatch):
    mem_tables(monkeypatch)
    real = DocTable.aload
    calls = []

    async def aload(self, *, force=False):
        calls.append(1)
        if len(calls) == 1:
            raise asyncio.CancelledError()
        return await real(self, force=force)
    monkeypatch.setattr(DocTable, "aload", aload)
    t = DocTable("saved_searches")
    t.start_background_load()
    assert _wait(lambda: t.loaded), "the loader thread died on a CancelledError"


def _no_tracebacks(caplog):
    bad = [r for r in caplog.records if r.exc_info]
    assert not bad, "a traceback for a loading table: %s" % [r.getMessage() for r in bad]


def test_the_scheduled_posts_pass_skips_a_loading_table_in_one_line(monkeypatch, caplog):
    from app.services import scheduled_posts_service as sp

    async def loading():
        raise Loading("table scheduled_posts is still loading")
    monkeypatch.setattr(sp, "_table", loading)
    caplog.set_level(logging.INFO)
    asyncio.run(sp._publish_due_once())
    _no_tracebacks(caplog)
    assert any("still loading" in r.getMessage() for r in caplog.records)


def test_the_bot_seed_check_waits_for_a_loading_table_quietly(monkeypatch, caplog, tmp_path):
    from app.services import bot_manager_service as bm
    from app.services import bot_table
    from app.services.relay_reader import Unavailable
    export = tmp_path / "bots_config_export.json"
    export.write_text("{}")
    monkeypatch.setattr(bm, "_EXPORT_PATH", export)

    def all_bots(db=None):
        raise Unavailable("table bots is still loading")
    monkeypatch.setattr(bot_table, "all_bots", all_bots)
    caplog.set_level(logging.INFO)
    bm.seed_from_export()
    _no_tracebacks(caplog)
    assert bm._seed["pending"] is True, "the seed check is not asked again"
    monkeypatch.setattr(bot_table, "all_bots", lambda db=None: [object()])
    bm.seed_from_export()
    assert bm._seed["pending"] is False


def test_the_instance_application_retries_skip_a_loading_table_quietly(monkeypatch, caplog):
    from app.services import instance_welcome as iw
    from app.services.relay_reader import Unavailable
    jobs = []

    class Sched:
        def add_job(self, fn, *a, **k):
            jobs.append(fn)

        def start(self):
            pass

    import apscheduler.schedulers.asyncio as aps
    monkeypatch.setattr(aps, "AsyncIOScheduler", Sched)
    monkeypatch.setattr(iw, "_scheduler", None)

    async def retry(db):
        raise Unavailable("table instance_applications is still loading")
    monkeypatch.setattr(iw, "retry_notifications", retry)
    iw.start_instance_welcome_scheduler()
    caplog.set_level(logging.INFO)
    asyncio.run(jobs[0]())
    _no_tracebacks(caplog)
