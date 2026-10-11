"""Admin → Relay → Limits / Database: the numeric relay + PosterChanDB knobs.

"I don't see any DB tunables for PosterChanDB in admin either" (2026-10-11). What this holds:
  * every default is the value the code used BEFORE it was a setting -- pinned here as literals, so a node that
    never opens the Database or Limits section runs exactly as it did;
  * the schema, the relay config and the admin save all read ONE table (relay_tunables), and the relay actually
    APPLIES each value to the object that uses it (not just reads it into a dict);
  * an out-of-range value is refused by Save with nothing stored, and one stored before the range existed is
    clamped when the relay reads it;
  * the bug this found: the relay process opens its PosterChanDB store BEFORE it can read a setting, so on a
    primary node (every fresh install, and both production nodes) "Write delay" and "Read cache" were never
    applied at all -- the store always ran on 300 s and automatic;
  * and the Database status line always read "Off.": relay_status() never passed `posterchandb` through.
"""
import json
import os
from unittest import mock

import pytest
from fastapi import HTTPException

from app.services import relay_tunables as rt
from app.services import settings_store

# What the code used before these were settings (posterchandb/store.py, cache.py, maintenance.py,
# nostr_relay/pcdb_store.py, nostr_relay/thread.py). Literals on purpose: a default drifting is the regression.
PREVIOUS = {
    "posterchandb_flush_seconds": 300,
    "posterchandb_read_cache_mb": 0,
    "posterchandb_cache_low_water_mb": 0,
    "posterchandb_snapshot_hours": 1.0,
    "posterchandb_compact_dead_pct": 40,
    "posterchandb_maint_interval_sec": 600,
    "posterchandb_maint_io_mb_s": 8.0,
    "posterchandb_maint_cpu_pct": 10,
    "posterchandb_maint_busy_load": 0.75,
    "posterchandb_words_batch": 25,
    "nostr_relay_posts_per_min": 10,
    "nostr_relay_same_per_hour": 3,
    "nostr_relay_gift_wraps_per_recipient": 300,
    "nostr_relay_max_connections": 5000,
    "nostr_relay_max_subs_per_conn": 500,
    "nostr_relay_max_filters_per_req": 25,
    "nostr_relay_max_message_kb": 512,
}


def test_defaults_are_what_the_code_used_before():
    assert rt.DEFAULTS == PREVIOUS


def test_the_code_defaults_still_agree():
    """The objects' own constructor defaults and the relay constants are the same numbers as the table."""
    from app.services.nostr_relay import thread
    from app.services.posterchandb import cache, maintenance, store
    import inspect
    assert thread._MAX_MESSAGE_SIZE == PREVIOUS["nostr_relay_max_message_kb"] * 1024
    assert thread._MAX_SUBS_PER_CONN == PREVIOUS["nostr_relay_max_subs_per_conn"]
    assert thread._MAX_FILTERS_PER_REQ == PREVIOUS["nostr_relay_max_filters_per_req"]
    p = inspect.signature(store.Store.__init__).parameters
    assert p["flush_interval"].default == PREVIOUS["posterchandb_flush_seconds"]
    assert p["compact_dead_pct"].default == PREVIOUS["posterchandb_compact_dead_pct"]
    m = inspect.signature(maintenance.Maintainer.__init__).parameters
    assert m["interval"].default == PREVIOUS["posterchandb_maint_interval_sec"]
    assert m["io_mb_s"].default == PREVIOUS["posterchandb_maint_io_mb_s"]
    assert m["cpu_pct"].default == PREVIOUS["posterchandb_maint_cpu_pct"]
    assert m["busy_load"].default == PREVIOUS["posterchandb_maint_busy_load"]
    g = inspect.signature(cache.CacheGovernor.__init__).parameters
    assert g["budget_mb"].default == PREVIOUS["posterchandb_read_cache_mb"]
    assert g["low_water_mb"].default == PREVIOUS["posterchandb_cache_low_water_mb"]


def test_the_schema_declares_every_knob_with_its_default():
    """Undeclared = never hydrates (CLAUDE.md); declared with another default = the field shows a value that is
    not the one in force."""
    from app.schemas import SettingsResponse
    fields = SettingsResponse.model_fields
    for k, v in PREVIOUS.items():
        assert k in fields, k
        assert fields[k].default == v, (k, fields[k].default, v)


@pytest.mark.parametrize("key,bad", [
    ("posterchandb_flush_seconds", "0"), ("posterchandb_flush_seconds", "3601"), ("posterchandb_flush_seconds", "abc"),
    ("posterchandb_flush_seconds", "2.5"), ("posterchandb_snapshot_hours", "0.1"), ("posterchandb_snapshot_hours", "nan"),
    ("posterchandb_compact_dead_pct", "95"), ("posterchandb_maint_cpu_pct", "0"), ("posterchandb_maint_io_mb_s", "-1"),
    ("posterchandb_words_batch", "100000"), ("posterchandb_read_cache_mb", "-5"),
    ("nostr_relay_max_connections", "1"), ("nostr_relay_max_message_kb", "1"), ("nostr_relay_max_subs_per_conn", "0"),
    ("nostr_relay_posts_per_min", "-1"),
])
def test_nonsense_is_rejected(key, bad):
    assert rt.validate(key, bad)


@pytest.mark.parametrize("key,ok", [
    ("posterchandb_flush_seconds", "1"), ("posterchandb_flush_seconds", "3600"), ("posterchandb_flush_seconds", "300.0"),
    ("posterchandb_snapshot_hours", "0.25"), ("posterchandb_maint_busy_load", "0"), ("posterchandb_read_cache_mb", "0"),
    ("nostr_relay_posts_per_min", "0"), ("nostr_relay_max_message_kb", "16384"), ("posterchandb_words_batch", ""),
    ("some_other_setting", "anything"),
])
def test_sane_values_and_blanks_pass(key, ok):
    assert rt.validate(key, ok) is None


def test_a_stored_out_of_range_value_is_clamped_not_dropped():
    assert rt.resolve("posterchandb_compact_dead_pct", "999") == 90
    assert rt.resolve("posterchandb_flush_seconds", "0") == 1
    assert rt.resolve("posterchandb_flush_seconds", "junk") == 300
    assert rt.resolve("posterchandb_flush_seconds", "") == 300
    assert rt.resolve("posterchandb_snapshot_hours", "6") == 6.0


def test_admin_save_refuses_out_of_range_and_stores_nothing(monkeypatch):
    from app.routers import admin
    from app.schemas import SettingsUpdate
    put = mock.Mock()
    monkeypatch.setattr(settings_store, "put", put)
    with pytest.raises(HTTPException) as ei:
        admin.update_settings(SettingsUpdate(settings={"posterchandb_compact_dead_pct": "5",
                                                       "nostr_relay_name": "fine"}),
                              db=mock.MagicMock(), admin=mock.Mock())
    assert ei.value.status_code == 400
    assert "posterchandb_compact_dead_pct" in ei.value.detail and "10" in ei.value.detail
    put.assert_not_called()


def test_a_tunable_is_a_live_reload_and_the_message_size_a_restart(monkeypatch):
    """Save routes a PosterChanDB tunable / posting limit to the live store-config reload (no outage), and the
    one limit websockets fixes at listen time to a restart."""
    from app.routers import admin
    from app.schemas import SettingsUpdate
    from app.services.nostr_relay import thread
    stored = {}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: stored.get(k, d))
    monkeypatch.setattr(settings_store, "put", lambda k, v, **kw: stored.__setitem__(k, v))

    async def _wt(db, d):
        return len(d)
    monkeypatch.setattr(settings_store, "write_through", _wt)
    reload_, restart = mock.Mock(), mock.Mock()
    monkeypatch.setattr(thread, "trigger_store_config_reload", reload_)
    monkeypatch.setattr(thread, "restart_nostr_relay", restart)
    admin.update_settings(SettingsUpdate(settings={"posterchandb_snapshot_hours": "6",
                                                   "nostr_relay_posts_per_min": "4"}),
                          db=mock.MagicMock(), admin=mock.Mock())
    assert reload_.called and stored["posterchandb_snapshot_hours"] == "6"
    import time
    admin.update_settings(SettingsUpdate(settings={"nostr_relay_max_message_kb": "1024"}),
                          db=mock.MagicMock(), admin=mock.Mock())
    for _ in range(50):
        if restart.called:
            break
        time.sleep(0.05)
    assert restart.called


def _read_config_with(monkeypatch, vals):
    from app.services.nostr_relay import thread
    import app.database as database
    monkeypatch.setattr(settings_store, "load_local", lambda: None)
    monkeypatch.setattr(settings_store, "hydrate_from_db", lambda db: None)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(database, "SessionLocal", lambda: mock.MagicMock())
    monkeypatch.setattr(thread, "_collect_operator_pubkeys", lambda db: [])
    monkeypatch.setattr(thread, "_collect_preserve_pubkeys", lambda db: [])
    monkeypatch.setattr(thread, "_node_pubkey", lambda: "")
    monkeypatch.setattr(thread, "_pcdb_mode", lambda s="": "primary")
    return thread._read_config()


def test_the_relay_config_reads_every_knob(monkeypatch):
    cfg = _read_config_with(monkeypatch, {"posterchandb_snapshot_hours": "6", "nostr_relay_max_message_kb": "1024",
                                          "nostr_relay_posts_per_min": "7", "posterchandb_compact_dead_pct": "999",
                                          "nostr_relay_max_subs_per_conn": "40", "posterchandb_words_batch": "100"})
    assert cfg["max_message_size"] == 1024 * 1024
    assert cfg["posts_per_min"] == 7 and cfg["max_subs_per_conn"] == 40
    assert cfg["max_filters_per_req"] == 25 and cfg["max_connections"] == 5000
    t = cfg["pcdb_tuning"]
    assert t["posterchandb_snapshot_hours"] == 6.0 and t["posterchandb_compact_dead_pct"] == 90
    assert t["posterchandb_words_batch"] == 100 and t["posterchandb_flush_seconds"] == 300


def test_with_nothing_stored_the_relay_config_is_unchanged(monkeypatch):
    cfg = _read_config_with(monkeypatch, {})
    assert (cfg["max_message_size"], cfg["max_subs_per_conn"], cfg["max_filters_per_req"]) == (512 * 1024, 500, 25)
    assert (cfg["posts_per_min"], cfg["same_per_hour"], cfg["gift_per_recipient"], cfg["max_connections"]) == (10, 3, 300, 5000)
    assert (cfg["pcdb_flush_s"], cfg["pcdb_cache_mb"]) == (300, 0)
    assert cfg["pcdb_tuning"] == {k: v for k, v in PREVIOUS.items() if k.startswith("posterchandb_")}


TUNED = {"posterchandb_flush_seconds": 45, "posterchandb_read_cache_mb": 256, "posterchandb_cache_low_water_mb": 900,
         "posterchandb_snapshot_hours": 6.0, "posterchandb_compact_dead_pct": 55, "posterchandb_maint_interval_sec": 120,
         "posterchandb_maint_io_mb_s": 32.0, "posterchandb_maint_cpu_pct": 25, "posterchandb_maint_busy_load": 2.0,
         "posterchandb_words_batch": 100}


def _assert_tuned(ps):
    st, gov, maint = ps.store, ps._gov, ps._maint
    assert st.flush_interval == 45 and st.compact_dead_pct == 55
    assert gov.budget_mb == 256 and gov.low_water_mb == 900
    assert maint.snapshot_hours == 6.0 and maint.interval == 120
    assert maint.throttle.io_bps == 32 * 1024 * 1024 and maint.throttle.cpu_pct == 25 and maint.throttle.busy_load == 2.0
    assert ps.words_batch == 100


def test_the_primary_store_opened_before_settings_takes_them_at_start(tmp_path):
    """THE BUG: relay_main opens the store with the defaults (open_store_for_settings), then reads the settings.
    _main must hand them to the store it was given -- before this, Write delay / Read cache did nothing on a
    primary node."""
    from app.services.nostr_relay import pcdb_store as P, thread
    d = str(tmp_path / "relay")
    P.init_new(d)
    ps = P.PcdbRelayStore(d)                          # what open_store_for_settings builds: defaults only
    ps.open(None)
    try:
        assert ps.store.flush_interval == 300 and ps._maint.snapshot_hours == 1.0
        with mock.patch.dict(thread._mirror_ref, {"primary": ps}):
            thread._configure_pcdb({"pcdb_tuning": dict(TUNED)})
        _assert_tuned(ps)
        assert ps.stats()["tuning"]["posterchandb_flush_seconds"] == 45     # the panel shows what is in force
    finally:
        ps.close()


def test_the_live_reload_applies_limits_and_tuning_to_the_running_relay(tmp_path):
    """Admin Save → reload-store-config → _apply_live_config: the running config (which the server and its
    SpamGuard hold) and the open store both change, no restart."""
    from app.services.nostr_relay import pcdb_store as P, thread
    from app.services.nostr_relay.spamguard import SpamGuard
    d = str(tmp_path / "relay")
    P.init_new(d)
    ps = P.PcdbRelayStore(d)
    ps.open(None)
    try:
        cfg = {"posts_per_min": 10, "max_connections": 5000, "max_subs_per_conn": 500, "retention_days": 30,
               "max_events": 0, "pcdb_tuning": {}}
        guard = SpamGuard(cfg)
        fresh = {"retention_days": 30, "max_events": 0, "paid_retention_enabled": False, "free_retention_days": 0,
                 "paid_retention_days": 0, "posts_per_min": 1, "same_per_hour": 3, "gift_per_recipient": 300,
                 "max_connections": 77, "max_subs_per_conn": 9, "max_filters_per_req": 4, "pcdb_tuning": dict(TUNED)}
        with mock.patch.dict(thread._mirror_ref, {"primary": ps}):
            thread._apply_live_config(ps, cfg, fresh)
        assert cfg["max_connections"] == 77 and cfg["max_subs_per_conn"] == 9 and cfg["max_filters_per_req"] == 4
        ev = {"kind": 1, "pubkey": "a" * 64, "created_at": 1000, "content": "hello there friend"}
        assert guard.check(ev) == "" and guard.check(dict(ev, content="another one")).startswith("rate-limited")
        _assert_tuned(ps)
    finally:
        ps.close()


def test_the_mirror_takes_the_tuning_too(tmp_path):
    from app.services.posterchandb import maintenance, mirror
    m = mirror.Mirror(str(tmp_path / "m"), "serve", lambda: None, maintenance=False)
    m.apply_tuning(dict(TUNED))
    assert m.flush_interval == 45 and m.cache_mb == 256 and m.words_batch == 100
    m._maint = maintenance.Maintainer(object(), snapshot_hours=1.0)
    m.apply_tuning({})
    assert m._maint.snapshot_hours == 6.0 and m._maint.throttle.cpu_pct == 25


def test_relay_status_passes_the_database_status_through(tmp_path, monkeypatch):
    """The Database status line read s.posterchandb, which relay_status() never copied out of the status file --
    so every node's panel said "Off."."""
    from app.services.nostr_relay import thread as th
    status = tmp_path / "status.json"
    pc = {"mode": "primary", "state": "ready", "live": 12, "dead": 1, "disk_bytes": 4096, "last_snapshot": 5}
    status.write_text(json.dumps({"running": True, "members": 3, "pid": os.getpid(), "ts": 10 ** 10,
                                  "posterchandb": pc}))
    monkeypatch.setattr(th, "_relay_db_path", lambda: str(tmp_path / "relay"))
    monkeypatch.setattr(th, "_relay_paths", lambda p: {"status": str(status), "control": str(tmp_path)})
    assert th.relay_status()["posterchandb"] == pc
