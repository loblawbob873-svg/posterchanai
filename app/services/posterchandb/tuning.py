"""Apply Admin → Relay → Database tunables (app/services/relay_tunables.PCDB) to a RUNNING store.

Every knob is an attribute its loop re-reads on each step -- the flush loop (Store.flush_interval, every second),
the cache governor (budget/low water, every tick), the housekeeper (interval, snapshot age, throttle, after each
wait) and compaction (compact_dead_pct, per candidate) -- so changing it takes effect without reopening anything.
Nothing here touches a file. `t` maps relay_tunables keys to already-resolved (range-checked) values; a key that
is absent is left as it is.
"""
from __future__ import annotations

MB = 1024 * 1024


def apply(store=None, gov=None, maint=None, t: dict | None = None) -> dict:
    """Returns what was applied ({setting key: value}), for the log line and the status panel."""
    t = dict(t or {})
    done = {}

    def take(key, fn):
        if key in t and t[key] is not None:
            fn(t[key])
            done[key] = t[key]

    if store is not None:
        take("posterchandb_flush_seconds", lambda v: setattr(store, "flush_interval", float(v)))
        take("posterchandb_compact_dead_pct", lambda v: setattr(store, "compact_dead_pct", float(v)))
    if gov is not None:
        take("posterchandb_read_cache_mb", lambda v: setattr(gov, "budget_mb", float(v or 0)))
        take("posterchandb_cache_low_water_mb", lambda v: setattr(gov, "low_water_mb", float(v or 0)))
    if maint is not None:
        take("posterchandb_snapshot_hours", lambda v: setattr(maint, "snapshot_hours", float(v)))
        take("posterchandb_maint_interval_sec", lambda v: setattr(maint, "interval", float(v)))
        th = getattr(maint, "throttle", None)
        if th is not None:
            take("posterchandb_maint_io_mb_s", lambda v: setattr(th, "io_bps", max(0.1, float(v)) * MB))
            take("posterchandb_maint_cpu_pct", lambda v: setattr(th, "cpu_pct", min(100.0, max(1.0, float(v)))))
            take("posterchandb_maint_busy_load", lambda v: setattr(th, "busy_load", float(v)))
    return done


def snapshot_of(store=None, gov=None, maint=None) -> dict:
    """The values IN FORCE, read back off the objects (not the settings): what the status panel shows."""
    out = {}
    if store is not None:
        out["posterchandb_flush_seconds"] = getattr(store, "flush_interval", None)
        out["posterchandb_compact_dead_pct"] = getattr(store, "compact_dead_pct", None)
    if gov is not None:
        out["posterchandb_read_cache_mb"] = getattr(gov, "budget_mb", None)
        out["posterchandb_cache_low_water_mb"] = getattr(gov, "low_water_mb", None)
    if maint is not None:
        out["posterchandb_snapshot_hours"] = getattr(maint, "snapshot_hours", None)
        out["posterchandb_maint_interval_sec"] = getattr(maint, "interval", None)
        th = getattr(maint, "throttle", None)
        if th is not None:
            out["posterchandb_maint_io_mb_s"] = round(th.io_bps / MB, 3)
            out["posterchandb_maint_cpu_pct"] = th.cpu_pct
            out["posterchandb_maint_busy_load"] = th.busy_load
    return out


def status_extras(store, gov, maint, **extra) -> dict:
    """What Admin → Relay → Database shows beyond the event counts: snapshot age, the RAM cache's last reading and
    the tunables IN FORCE (read off the objects, not the settings). All O(1): this runs every status write."""
    out = {}
    if store is not None:
        out["last_snapshot"] = int(getattr(store, "last_snapshot", 0) or 0)
    last = dict(getattr(gov, "last", None) or {}) if gov is not None else {}
    for k in ("available_mb", "resident_mb", "index_mb", "budget_mb", "psi"):
        if k in last:
            out["cache_" + k] = last[k]
    if maint is not None:
        out["maint_last"] = {k: v for k, v in (getattr(maint, "last", None) or {}).items()
                             if isinstance(v, (int, float, bool, str)) or v is None}
    tuned = snapshot_of(store, gov, maint)
    for k, v in extra.items():
        tuned["posterchandb_" + k] = v
    out["tuning"] = tuned
    return out
