"""The relay's numeric knobs that Admin → Relay exposes: their defaults, their safe ranges, and when they apply.

ONE table, read by three places that must never disagree:
  * app/schemas.py SettingsResponse declares each key with DEFAULTS[key] (tests/test_relay_tunables.py), so the
    admin field shows the value in force rather than a blank;
  * nostr_relay/thread.py:_read_config resolves each through `resolve` -- a stored value outside its range is
    CLAMPED (and logged), never handed to the store as-is and never silently replaced by the default;
  * routers/admin.py update_settings refuses (400, nothing stored) a value `validate` rejects, so nonsense
    never reaches the relay doc in the first place.

Every default here is the value the code used before it was a setting, so a node that never touches the
Database or Limits section behaves exactly as before.

`live`: applied to the running relay within seconds of Save (the reload-store-config control command,
thread._apply_live_config); otherwise Save restarts the relay. Deliberately NOT here: anything that names a path
(the data directory, the PRIMARY marker, the lock) or changes the on-disk format (segment size) -- those are not
tuning, and a wrong value loses data rather than speed.
"""
from __future__ import annotations

from collections import namedtuple

Knob = namedtuple("Knob", "default lo hi kind live unit")

# --- PosterChanDB (posterchandb/store.py, cache.py, maintenance.py, nostr_relay/pcdb_store.py) -------------
PCDB = {
    # Store.flush_interval: how long new events wait in RAM before one batched write. A crash loses this window.
    "posterchandb_flush_seconds": Knob(300, 1, 3600, int, True, "s"),
    # CacheGovernor.budget_mb: RAM for older segments' bytes. 0 = automatic (30% of RAM).
    "posterchandb_read_cache_mb": Knob(0, 0, 1048576, int, True, "MB"),
    # CacheGovernor.low_water_mb: below this much free RAM the cache drains. 0 = automatic (max(512 MB, 10%)).
    "posterchandb_cache_low_water_mb": Knob(0, 0, 1048576, int, True, "MB"),
    # Maintainer.snapshot_hours: an index snapshot at most this often (bounds what a restart replays).
    "posterchandb_snapshot_hours": Knob(1.0, 0.25, 48.0, float, True, "h"),
    # Store.compact_dead_pct: a closed segment this % dead is rewritten to free its disk and RAM.
    "posterchandb_compact_dead_pct": Knob(40, 10, 90, int, True, "%"),
    # Maintainer.interval: seconds between housekeeping passes (compaction, snapshot).
    "posterchandb_maint_interval_sec": Knob(600, 60, 86400, int, True, "s"),
    # Throttle.io_bps: maintenance write ceiling.
    "posterchandb_maint_io_mb_s": Knob(8.0, 1.0, 1000.0, float, True, "MB/s"),
    # Throttle.cpu_pct: maintenance CPU share.
    "posterchandb_maint_cpu_pct": Knob(10, 1, 100, int, True, "%"),
    # Throttle.busy_load: maintenance waits while the 1-minute load per CPU is above this. 0 = never waits.
    "posterchandb_maint_busy_load": Knob(0.75, 0.0, 16.0, float, True, "load/CPU"),
    # PcdbRelayStore._index_words batch: search words indexed per background step after a restart.
    "posterchandb_words_batch": Knob(25, 1, 2000, int, True, "events"),
}

# --- relay protocol / posting limits (nostr_relay/server.py, spamguard.py) --------------------------------
LIMITS = {
    "nostr_relay_posts_per_min": Knob(10, 0, 10000, int, True, "posts"),
    "nostr_relay_same_per_hour": Knob(3, 0, 10000, int, True, "posts"),
    "nostr_relay_gift_wraps_per_recipient": Knob(300, 0, 1000000, int, True, "events"),
    "nostr_relay_max_connections": Knob(5000, 10, 1000000, int, True, "sockets"),
    "nostr_relay_max_subs_per_conn": Knob(500, 1, 10000, int, True, "subscriptions"),
    "nostr_relay_max_filters_per_req": Knob(25, 1, 1000, int, True, "filters"),
    # websockets' max_size is fixed when the listener starts, so this one restarts the relay.
    "nostr_relay_max_message_kb": Knob(512, 64, 16384, int, False, "KB"),
}

ALL = {**PCDB, **LIMITS}
DEFAULTS = {k: v.default for k, v in ALL.items()}
LIVE_KEYS = frozenset(k for k, v in ALL.items() if v.live)
RESTART_KEYS = frozenset(k for k, v in ALL.items() if not v.live)


def _parse(knob: Knob, raw):
    s = str(raw).strip()
    if knob.kind is int:
        f = float(s)                     # "300" and "300.0" both mean 300; "3.5" does not
        if not f.is_integer():
            raise ValueError("a whole number")
        return int(f)
    return float(s)


def validate(key: str, raw) -> str | None:
    """None when `raw` may be stored for `key` (blank = "leave as it is"), else a sentence saying why not.
    Keys this module does not know are not its business (None)."""
    knob = ALL.get(key)
    if knob is None or raw is None or str(raw).strip() == "":
        return None
    try:
        v = _parse(knob, raw)
    except (TypeError, ValueError):
        return "%s must be %s" % (key, "a whole number" if knob.kind is int else "a number")
    if v != v or not (knob.lo <= v <= knob.hi):          # v != v: NaN
        return "%s must be between %s and %s %s" % (key, _fmt(knob.lo), _fmt(knob.hi), knob.unit)
    return None


def validate_many(settings: dict) -> list:
    return [e for e in (validate(k, v) for k, v in (settings or {}).items()) if e]


def resolve(key: str, raw, log=None):
    """The value in force for a stored `raw`: blank or unparseable = the default; out of range = the nearest
    end of the range (a value stored before the range existed keeps its intent as far as is safe)."""
    knob = ALL[key]
    if raw is None or str(raw).strip() == "":
        return knob.default
    try:
        v = _parse(knob, raw)
    except (TypeError, ValueError):
        if log:
            log("[relay-tunables] %s=%r is not a number -- using the default %s" % (key, raw, knob.default))
        return knob.default
    if v != v:
        return knob.default
    c = min(knob.hi, max(knob.lo, v))
    if c != v and log:
        log("[relay-tunables] %s=%s is outside %s..%s -- using %s" % (key, v, knob.lo, knob.hi, c))
    return knob.kind(c)


def read(get, keys=None, log=None) -> dict:
    """{key: value in force} for `keys` (default: all), where get(key) returns the stored raw value or None."""
    return {k: resolve(k, get(k), log=log) for k in (keys or ALL)}


def _fmt(x) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)
