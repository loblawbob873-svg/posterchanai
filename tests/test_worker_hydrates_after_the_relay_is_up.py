"""The worker's schedulers start with the RELAY's settings, not the defaults -- even when the relay could not
answer the worker's first hydrate. Measured after deploy 211c: the worker hydrated 0 settings (relay still
booting), started every scheduler, and ActivityPub delivery reported "off" (no domain) until the 120s
re-hydrate. Drives the shipped app/worker.py:_run with a relay that answers only on the third ask."""
import asyncio
import sys
import types


def test_schedulers_start_with_hydrated_settings_when_the_first_hydrate_found_nothing(monkeypatch):
    from app import worker
    from app.services import settings_store
    calls, seen = [], []
    def hydrate(db):
        calls.append(1)
        if len(calls) < 3:
            return 0                      # relay listening but still loading: nothing yet
        settings_store._CACHE["activitypub_domain"] = "poster.example"
        return 486
    monkeypatch.setattr(settings_store, "hydrate_from_db", hydrate)
    monkeypatch.setattr(settings_store, "load_local", lambda: None)
    monkeypatch.setattr(settings_store, "_CACHE", dict(settings_store._CACHE))
    monkeypatch.setitem(sys.modules, "app.database", types.SimpleNamespace(SessionLocal=lambda: types.SimpleNamespace(close=lambda: None)))
    async def no_wait(timeout=60.0):
        return None
    monkeypatch.setattr(worker, "_wait_for_relay", no_wait)
    fake = types.ModuleType("fake_sched")
    fake.start = lambda: seen.append(settings_store.get("activitypub_domain"))
    monkeypatch.setitem(sys.modules, "fake_sched", fake)
    monkeypatch.setattr(worker, "_SCHEDULERS", [("fake", "fake_sched", "start")])
    real_sleep = asyncio.sleep
    async def fast_sleep(s, *a, **k):
        await real_sleep(0)
    monkeypatch.setattr(worker.asyncio, "sleep", fast_sleep)

    async def go():
        task = asyncio.ensure_future(worker._run())
        for _ in range(2000):
            if seen:
                break
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    asyncio.run(go())
    assert seen == ["poster.example"], (seen, len(calls))
