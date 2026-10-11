"""Every admin setting added on 2026-10-10/11 PERSISTS and HYDRATES -- end to end, not through a patched getter.

The keys: `tor_extra_daemons` (Admin → Network), `posterchandb_mode` and the 17 relay tunables of
app/services/relay_tunables.py (Admin → Relay → Limits / Database). For each one, through the SHIPPED code:

  1. SAVE: PUT /api/admin/settings on the real admin router (admin auth: get_admin_user runs, only the session
     lookup is stubbed) with a non-default value → the settings store holds it AND the relay received an
     operator-signed (signature checked), NIP-44-encrypted `pcai:setting:<key>` document carrying it → GET says so.
  2. HYDRATE in a FRESH PROCESS (tests/admin_settings_fresh_process.py): init_db() defaults, then
     settings_store.hydrate_from_db() over the relay socket -- nothing in that process was ever written by the
     Save -- and GET /api/admin/settings returns every value.
  3. CONSUMER, in that same fresh process: tor_service.pool_plan() runs that many extra daemons, and the relay's
     own thread._read_config() (which does its OWN hydrate) carries every limit and tunable; the tunables then
     reach a real PosterChanDB store through _configure_pcdb.
  4. BLANK / OUT OF RANGE: blank = leave as it is (numbers) or the default (text); out-of-range is refused with
     a 400 and NOTHING reaches the store or the relay; a value stored before the range existed is clamped by
     the relay, and a stored value the schema cannot parse never takes GET (the whole admin page) down.

The relay is tests/relay_fake.FakeRelay on a real socket (it now accepts writes: signature-checked, NIP-33
replacement). The admin page's own round trip (number "0", "auto", select, reload) is
scripts/check_admin_settings_persist.py in a real Chrome (tests/test_admin_settings_persist_browser.py).
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import relay_tunables, settings_store as S
from app.services.nostr import bip340, nip44
from app.services.nostr.event import verify_event
from tests.relay_fake import FakeRelay

ROOT = Path(__file__).resolve().parents[1]
OP_SK = bytes.fromhex("5a" * 32)
OP = bip340.pubkey_from_seckey(OP_SK).hex()

# A non-default value for every key added in the last two days -- including the edges a generic form drops:
# a ZERO (falsy) for a limit and a load, a fraction for a float, a ceiling, and a select's non-first option.
VALUES = {
    "tor_extra_daemons": "3",
    "posterchandb_mode": "shadow",
    "posterchandb_flush_seconds": "45",
    "posterchandb_read_cache_mb": "256",
    "posterchandb_cache_low_water_mb": "900",
    "posterchandb_snapshot_hours": "0.5",
    "posterchandb_compact_dead_pct": "55",
    "posterchandb_maint_interval_sec": "120",
    "posterchandb_maint_io_mb_s": "32.5",
    "posterchandb_maint_cpu_pct": "25",
    "posterchandb_maint_busy_load": "0",
    "posterchandb_words_batch": "100",
    "nostr_relay_posts_per_min": "0",
    "nostr_relay_same_per_hour": "7",
    "nostr_relay_gift_wraps_per_recipient": "1000",
    "nostr_relay_max_connections": "250",
    "nostr_relay_max_subs_per_conn": "40",
    "nostr_relay_max_filters_per_req": "12",
    "nostr_relay_max_message_kb": "16384",
}


def test_the_audit_covers_every_key_added_in_the_last_two_days():
    """If a key joins relay_tunables (or another of today's settings) it must join this audit too."""
    assert set(relay_tunables.ALL) <= set(VALUES)
    assert len(VALUES) == 19
    from app.schemas import SettingsResponse
    for k, v in VALUES.items():
        assert k in SettingsResponse.model_fields, k
        assert str(SettingsResponse.model_fields[k].default) != v, f"{k}: the audit value must differ from the default"


def _typed(k, v):
    knob = relay_tunables.ALL.get(k)
    return v if knob is None else knob.kind(float(v))


@pytest.fixture
def node(monkeypatch):
    """A node: a relay on a real socket, a COLD settings store (nothing cached, never hydrated), the operator key,
    and the admin router behind its real get_admin_user (only the session -> User lookup is stubbed)."""
    relay = FakeRelay(operator=OP)
    monkeypatch.setattr(S, "_CACHE", {})
    monkeypatch.setattr(S, "_HYDRATED", False)
    monkeypatch.setattr(S, "_HYDRATED_KEYS", set())
    monkeypatch.setattr(S, "_LOCAL_KEYS", set())
    monkeypatch.setattr(S, "_EVENT_SOURCE", None)
    monkeypatch.setattr(S, "_OP_SK", OP_SK)
    monkeypatch.setattr(S, "_operator_seckey", lambda db: OP_SK)
    monkeypatch.setattr(S, "_port", lambda db=None: relay.port)
    monkeypatch.setattr(S, "_save_local_file", lambda: None)

    # The background writer is a process-wide daemon thread whose retries would outlive this test (and reach
    # whatever _port() says then), so run the SAME write_through it runs, synchronously, to the same relay.
    # Each asyncio.run is a new loop, and nostr_store pools one publish socket per loop; close it as the loop
    # ends (the real writer keeps ONE loop for its life) or every write leaves a socket the relay waits out.
    from app.services import nostr_store
    real_wt = S.write_through

    async def write_through(db, changes):
        try:
            return await real_wt(db, changes)
        finally:
            loop = asyncio.get_running_loop()
            for k, (lp, ws) in list(nostr_store._pub_pool.items()):
                if lp is loop:
                    nostr_store._pub_pool.pop(k, None)
                    await ws.close()
    monkeypatch.setattr(S, "write_through", write_through)

    def write_now(changes):
        asyncio.run(S.write_through(None, changes))
    monkeypatch.setattr(S, "_schedule_relay_write", write_now)
    from app.services.nostr_relay import thread
    reloads, restarts = [], []
    monkeypatch.setattr(thread, "trigger_store_config_reload", lambda *a, **k: reloads.append(1))
    monkeypatch.setattr(thread, "restart_nostr_relay", lambda *a, **k: restarts.append(1))

    from app.auth import get_current_user
    from app.database import get_db
    from app.routers import admin as admin_router
    app = FastAPI()
    app.include_router(admin_router.router)
    app.dependency_overrides[get_current_user] = lambda: mock.Mock(is_admin=True)
    app.dependency_overrides[get_db] = lambda: iter([object()])
    client = TestClient(app, raise_server_exceptions=False)
    yield relay, client, reloads, restarts
    relay.close()


def _relay_value(relay, key):
    """What the relay holds for `key`: the newest operator-signed, NIP-44 document, decrypted. None = no doc."""
    docs = [e for e in relay.events if e["kind"] == 30078 and e["pubkey"] == OP
            and ["d", "pcai:setting:" + key] in [list(t) for t in e["tags"]]]
    if not docs:
        return None
    assert len(docs) == 1, f"{key}: the relay kept {len(docs)} versions of a replaceable document"
    assert verify_event(docs[0]), f"{key}: not a valid operator signature"
    return json.loads(nip44.decrypt_self(OP_SK, docs[0]["content"]))["value"]


def _fresh_process(relay):
    r = subprocess.run([sys.executable, str(ROOT / "tests" / "admin_settings_fresh_process.py"), str(relay.port),
                        OP_SK.hex()], cwd=ROOT, capture_output=True, text=True, timeout=180)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, f"the fresh process did not answer (rc={r.returncode}):\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}"
    return json.loads(line[len("RESULT "):])


def test_save_then_a_fresh_process_hydrates_every_key_and_its_consumer_uses_it(node, tmp_path):
    relay, client, reloads, restarts = node
    r = client.put("/api/admin/settings", json={"settings": VALUES})
    assert r.status_code == 200, r.text

    # 1. Saved: the store, the relay document, and GET.
    got = client.get("/api/admin/settings")
    assert got.status_code == 200, got.text
    for k, v in VALUES.items():
        assert S.get(k) == v, f"{k}: the settings store holds {S.get(k)!r}"
        assert _relay_value(relay, k) == v, f"{k}: the relay holds {_relay_value(relay, k)!r}, not {v!r}"
        assert got.json()[k] == _typed(k, v), f"{k}: GET says {got.json()[k]!r}"
    assert restarts, "the message size / mode change must restart the relay (websockets fixes max_size at listen)"

    # 2. A FRESH process (cache = defaults, filled only from the relay).
    fresh = _fresh_process(relay)
    assert fresh["is_hydrated"] and fresh["hydrated"] >= len(VALUES), fresh
    assert fresh["status"] == 200, fresh["get"]
    for k, v in VALUES.items():
        assert fresh["get"][k] == _typed(k, v), f"{k}: a restarted node's admin page shows {fresh['get'][k]!r}"

    # 3. The consumers, in that fresh process.
    assert fresh["pool"] == 3, "tor_extra_daemons=3 did not reach tor_service.pool_plan after a restart"
    rc = fresh["relay"]
    assert rc["pcdb_mode"] == "shadow"
    assert (rc["posts_per_min"], rc["same_per_hour"], rc["gift_per_recipient"]) == (0, 7, 1000)
    assert (rc["max_connections"], rc["max_subs_per_conn"], rc["max_filters_per_req"]) == (250, 40, 12)
    assert rc["max_message_size"] == 16384 * 1024
    assert (rc["pcdb_flush_s"], rc["pcdb_cache_mb"]) == (45, 256)
    want = {k: _typed(k, VALUES[k]) for k in relay_tunables.PCDB}
    assert rc["pcdb_tuning"] == want

    # ... and the hydrated tunables reach a real PosterChanDB store (what _main does with that config).
    from app.services.nostr_relay import pcdb_store as P, thread
    d = str(tmp_path / "relay")
    P.init_new(d)
    ps = P.PcdbRelayStore(d)
    ps.open(None)
    try:
        with mock.patch.dict(thread._mirror_ref, {"primary": ps}):
            thread._configure_pcdb({"pcdb_tuning": rc["pcdb_tuning"]})
        assert ps.store.flush_interval == 45 and ps.store.compact_dead_pct == 55
        assert ps._gov.budget_mb == 256 and ps._gov.low_water_mb == 900
        assert ps._maint.snapshot_hours == 0.5 and ps._maint.interval == 120
        th = ps._maint.throttle
        assert th.io_bps == 32.5 * 1024 * 1024 and th.cpu_pct == 25 and th.busy_load == 0.0
        assert ps.words_batch == 100
    finally:
        ps.close()

    # ... and the limits reach the relay's spam guard (the server holds this same dict).
    from app.services.nostr_relay.spamguard import SpamGuard
    g = SpamGuard(dict(rc))
    ev = {"kind": 1, "pubkey": "a" * 64, "created_at": 1000, "content": "the same words again"}
    assert all(g.check(dict(ev, content="post %d here" % i)) == "" for i in range(30)), "posts_per_min 0 = off"


def test_a_live_tunable_alone_reloads_the_running_relay_without_a_restart(node):
    relay, client, reloads, restarts = node
    r = client.put("/api/admin/settings", json={"settings": {"posterchandb_words_batch": "300",
                                                             "nostr_relay_max_subs_per_conn": "77"}})
    assert r.status_code == 200, r.text
    assert reloads and not restarts
    assert _relay_value(relay, "posterchandb_words_batch") == "300", "the reload must find it on the relay"


@pytest.mark.parametrize("key,bad", [
    ("posterchandb_compact_dead_pct", "5"), ("nostr_relay_max_message_kb", "99999"),
    ("posterchandb_flush_seconds", "abc"), ("posterchandb_snapshot_hours", "nan"),
    ("tor_extra_daemons", "9"), ("tor_extra_daemons", "-1"), ("tor_extra_daemons", "lots"),
])
def test_out_of_range_is_refused_and_nothing_is_stored_anywhere(node, key, bad):
    relay, client, _, _ = node
    r = client.put("/api/admin/settings", json={"settings": {key: bad, "site_name": "unchanged too"}})
    assert r.status_code == 400, f"{key}={bad!r} was accepted ({r.status_code})"
    assert key in r.json()["detail"]
    assert S.get(key) is None and S.get("site_name") is None, "a refused Save stored part of itself"
    assert relay.writes == [], "a refused Save wrote to the relay"


@pytest.mark.parametrize("key,raw,stored", [
    ("posterchandb_flush_seconds", "1e3", "1000"),       # a browser number input sends this as-is
    ("posterchandb_flush_seconds", "300.0", "300"),
    ("nostr_relay_max_connections", " 250 ", "250"),
    ("posterchandb_snapshot_hours", "1.50", "1.5"),
    ("tor_extra_daemons", " AUTO ", "auto"),
    ("tor_extra_daemons", "08", "8"),
])
def test_an_accepted_number_is_stored_in_the_form_get_can_read_back(node, key, raw, stored):
    """"1e3" passed the range check (it is 1000) and was stored verbatim -- and SettingsResponse cannot parse "1e3"
    as an int, so GET /api/admin/settings answered 500 and the WHOLE admin page stopped loading."""
    relay, client, _, _ = node
    assert client.put("/api/admin/settings", json={"settings": {key: raw}}).status_code == 200
    g = client.get("/api/admin/settings")
    assert g.status_code == 200, f"after saving {key}={raw!r} the admin page cannot load: {g.text[-200:]}"
    assert S.get(key) == stored and _relay_value(relay, key) == stored


def test_blank_means_leave_it_for_a_number_and_the_default_for_tor(node):
    relay, client, _, _ = node
    assert client.put("/api/admin/settings", json={"settings": {"posterchandb_flush_seconds": "45",
                                                                "tor_extra_daemons": "3"}}).status_code == 200
    assert client.put("/api/admin/settings", json={"settings": {"posterchandb_flush_seconds": "",
                                                                "tor_extra_daemons": ""}}).status_code == 200
    assert S.get("posterchandb_flush_seconds") == "45", "a blank number field must leave the stored value"
    assert _relay_value(relay, "tor_extra_daemons") == "", "tor_extra_daemons is text: blank is a real clear"
    from app.services import tor_service
    assert len(tor_service.pool_plan(cpus=12)) == len(tor_service.pool_plan(
        mock.Mock(get=lambda k, d=None: "auto" if k == "tor_extra_daemons" else d,
                  get_bool=lambda k, d=False: d, get_int=lambda k, d=0: d), cpus=12)), "blank must mean auto"


def test_nothing_stored_shows_and_runs_the_defaults(node):
    _, client, _, _ = node
    g = client.get("/api/admin/settings").json()
    for k, knob in relay_tunables.ALL.items():
        assert g[k] == knob.default, k
    assert g["tor_extra_daemons"] == "auto" and g["posterchandb_mode"] in (None, "")


def test_a_stored_value_from_before_the_ranges_is_clamped_by_the_relay_and_never_breaks_the_page(node):
    """Values on the relay that no Save today would accept: one out of range (stored before the range existed),
    one the schema cannot parse at all. The relay clamps / defaults them; GET must still answer, with the
    unparseable one shown as its default -- one bad document must not blank every field on the admin page
    (a blank page then Saves `false` over every checkbox)."""
    relay, client, _, _ = node
    for k, v in (("posterchandb_compact_dead_pct", "999"), ("posterchandb_flush_seconds", "junk"),
                 ("nostr_relay_max_subs_per_conn", "2.5")):
        asyncio.run(S.write_through(None, {k: v}))
    S.hydrate_from_db(None)
    g = client.get("/api/admin/settings")
    assert g.status_code == 200, g.text[-400:]
    assert g.json()["posterchandb_flush_seconds"] == 300
    assert g.json()["nostr_relay_max_subs_per_conn"] == 500
    assert g.json()["posterchandb_compact_dead_pct"] == 999, "an out-of-range stored value is shown as stored"
    tun = relay_tunables.read(lambda k: S.get(k, None))
    assert tun["posterchandb_compact_dead_pct"] == 90 and tun["posterchandb_flush_seconds"] == 300
    assert tun["nostr_relay_max_subs_per_conn"] == 500
