"""Settings keep trying to load until the relay answers (2026-10-10).

After a deploy restarted the relay and the app together, both startup hydrates ran while the relay was still
coming up ("relay not readable yet") and nothing tried again: settings stayed unloaded, instance membership read
as unknown, and Office/Mail/Files refused every member for ~15 minutes until the app was restarted by hand."""
import threading
import time

from app.services import relay_reader, settings_store


def test_a_hydrate_that_failed_at_startup_is_retried_until_it_lands(monkeypatch):
    calls = {"n": 0}

    def events(sk, pk):
        calls["n"] += 1
        if calls["n"] < 3:
            raise relay_reader.Unavailable("relay starting")
        return {}

    monkeypatch.setattr(settings_store, "_operator_seckey", lambda db: b"\x01" * 32)
    monkeypatch.setattr(settings_store, "_operator_hex", lambda sk: "a" * 64)
    monkeypatch.setattr(settings_store, "_operator_setting_events", events)
    monkeypatch.setattr(settings_store, "_HYDRATED", False)

    class DB:
        def close(self): pass

    t = settings_store.ensure_hydrated_background(lambda: DB(), first_delay=0.01, max_delay=0.05)
    deadline = time.time() + 5
    while not settings_store.is_hydrated() and time.time() < deadline:
        time.sleep(0.02)
    assert settings_store.is_hydrated(), ("settings never loaded after the relay came up", calls)
    assert calls["n"] >= 3
    t.join(timeout=2)
    assert not t.is_alive(), "the retry keeps running after settings loaded"


def test_startup_arms_the_retry():
    src = open("app/main.py").read()
    i = src.index("settings_store.migrate_legacy_table(_db)")
    assert "ensure_hydrated_background(" in src[i - 600:i + 200], "startup does not retry a failed settings hydrate"
