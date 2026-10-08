"""Saving a web-of-trust setting changes the next rebuild -- and a deliberately smaller graph is allowed to shrink.

"WoT depth = 1 but clicking refresh WoT says depth 2 takes a few min" (2026-10-08). Three faults, one symptom:
the relay read the WoT settings once at start and nothing that saved them restarted it; the partial-crawl shrink
guard (keep the cache if a crawl comes back < 85%) would have refused depth 3 -> 1 even after a restart, since
that change halves the graph on purpose; and the Refresh button was throttled to one build per 30 minutes while
saying "started", with a hard-coded "depth 2" in its message.
"""
import asyncio
import json
from pathlib import Path
from unittest import mock

from app.services.nostr_relay import thread

ROOT = Path(__file__).resolve().parents[1]


def test_only_a_stricter_shape_lets_the_graph_shrink():
    old = {"wot_depth": 3, "wot_min_followers": 3, "wot_max": 0}
    assert thread._wot_stricter({"wot_depth": 1, "wot_min_followers": 3, "wot_max": 0}, old)
    assert thread._wot_stricter({"wot_depth": 3, "wot_min_followers": 4, "wot_max": 0}, old)
    assert thread._wot_stricter({"wot_depth": 3, "wot_min_followers": 3, "wot_max": 20000}, old)
    assert not thread._wot_stricter(dict(old), old), "the same settings must keep the partial-crawl guard"
    assert not thread._wot_stricter({"wot_depth": 3, "wot_min_followers": 2, "wot_max": 0}, old)
    assert not thread._wot_stricter({"wot_depth": 1}, None), "no record of what built the cache = guard stays on"


def _run_build(tmp_path, fresh, recorded, members=500):
    db = tmp_path / "relay.db"
    if recorded is not None:
        Path(str(db) + ".wot_shape").write_text(json.dumps(recorded))
    gate = mock.Mock()
    gate.last_build_partial = False
    gate.build = mock.AsyncMock(return_value=members)
    cfg = {"upstream": [], "seeds": ["s"], "operator": [], "direct": False, "author_batch": 200,
           "request_pace_sec": 0, "wot_depth": 3, "wot_min_followers": 3, "wot_max": 0, "wot_depth3_crawl_max": 2500}
    with mock.patch.object(thread, "_read_config", return_value=fresh), \
         mock.patch.object(thread, "_relay_db_path", return_value=str(db)), \
         mock.patch.object(thread, "_write_wot_stamp", lambda c: None):
        asyncio.run(thread._build_wot(gate, mock.Mock(), cfg))
    kw = gate.build.call_args.kwargs
    shape = Path(str(db) + ".wot_shape")
    return kw, (json.loads(shape.read_text()) if shape.exists() else None)


def test_a_saved_depth_is_used_by_the_next_rebuild_without_a_restart(tmp_path):
    kw, shape = _run_build(tmp_path, {"wot_depth": 1, "wot_min_followers": 3, "wot_max": 0},
                           {"wot_depth": 3, "wot_min_followers": 3, "wot_max": 0})
    assert kw["depth"] == 1, "the rebuild crawled the depth the relay started with"
    assert kw["min_keep_ratio"] == 0.0, "the shrink guard refused a deliberately smaller graph"
    assert shape and shape["wot_depth"] == 1, "a clean build must record what built it"


def test_unchanged_settings_keep_the_partial_crawl_guard(tmp_path):
    kw, _ = _run_build(tmp_path, {"wot_depth": 3, "wot_min_followers": 3, "wot_max": 0},
                       {"wot_depth": 3, "wot_min_followers": 3, "wot_max": 0})
    assert kw["depth"] == 3 and kw["min_keep_ratio"] == 0.85


def test_an_admin_refresh_is_not_throttled_and_saving_a_wot_setting_rebuilds():
    with mock.patch.object(thread, "_drop_control", lambda cmd: cmd):
        assert thread.trigger_wot_refresh(force=True) == {"cmd": "refresh-wot", "force": True}
        assert thread.trigger_wot_refresh() == {"cmd": "refresh-wot", "force": False}
    src = (ROOT / "app/services/nostr_relay/thread.py").read_text()
    assert 'not cmd.get("force") and _now - _st["last"]' in src, "the throttle must not apply to an admin's refresh"
    admin = (ROOT / "app/routers/admin.py").read_text()
    i = admin.index('"nostr_relay_wot_depth", "nostr_relay_wot_min_followers"')
    assert "trigger_wot_refresh(force=True)" in admin[i:i + 400]


def test_the_refresh_message_names_the_depth_it_rebuilds_at():
    html = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text()
    assert "building (depth 2 takes a few min)'" not in html, "the message is still hard-coded"
    assert "rebuilding at depth '+d" in html
