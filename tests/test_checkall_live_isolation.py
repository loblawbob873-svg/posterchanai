"""The release suite must not manufacture relay failures by load-testing its own live checks."""
import importlib.util
from pathlib import Path
import sys

import pytest


def _checkall():
    path = Path(__file__).parents[1] / "scripts" / "checkall.py"
    spec = importlib.util.spec_from_file_location("posterchan_checkall", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cold_session_stability_runs_outside_the_parallel_relay_batch():
    check = _checkall().CHECKS["check_search_profile_stability"]
    assert check["group"] == "live"
    assert check["serial"] is True
    assert check["live_args"] == ["5", "{live}"]


def test_two_browser_qr_login_remains_isolated_too():
    check = _checkall().CHECKS["check_qr_device_login"]
    assert check["serial"] is True


def test_full_sync_is_registered_live_and_isolated():
    check = _checkall().CHECKS["check_sync_full"]
    assert check["group"] == "live"
    assert check["serial"] is True
    assert check["live_args"] == ["{live}"]


def test_instance_backed_visual_checks_never_silently_use_their_defaults():
    checks = _checkall().CHECKS
    for name in ("check_user_settings_tabs", "check_timeline_uniformity", "check_qr_scan"):
        assert checks[name]["group"] == "live"
    # Five camera/browser scenarios can starve alongside the parallel live batch and turn a login
    # timeout into a misleading scanner skip.
    assert checks["check_qr_scan"]["serial"] is True


def test_installed_account_gate_keeps_the_external_electron_port():
    module = _checkall()
    check = module.CHECKS["check_installed_desktop_account"]
    assert check["serial"] is True
    assert check["env"]["PC_CHECK_PORT"] == "9223"
    discovered = next(c for c in module.discover()
                      if c["name"] == "check_installed_desktop_account")
    assert discovered["env"]["PC_CHECK_PORT"] == "9223"


@pytest.mark.parametrize("live", [False, True])
def test_real_service_checks_require_explicit_live_mode(tmp_path, monkeypatch, live):
    module = _checkall()
    names = {"check_drive_fresh_pair", "check_sync_card", "check_concord_live_invite",
             "check_signer_transport", "check_websearch_rate"}
    jobs = [job for job in module.discover() if job["name"] in names]
    assert len(jobs) == len(names)
    calls = []
    monkeypatch.setattr(module, "discover", lambda: jobs)
    monkeypatch.setattr(module, "SUITES", [])
    monkeypatch.setattr(module, "have_chrome", lambda: "/test/chrome")
    monkeypatch.setattr(module, "have_node", lambda: "/test/node")
    monkeypatch.setattr(module, "_captured", lambda argv, *args: (calls.append(argv) or (0, "OK")))
    argv = ["checkall", "--tmp", str(tmp_path), "--jobs", "1"]
    if live:
        argv += ["--live", "https://test.invalid"]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    if not live:
        assert calls == [], "the default suite started real service traffic"
        return
    assert len(calls) == len(names)
    for call in calls:
        name = Path(call[1]).stem
        expected = ["https://test.invalid"] if name in {"check_drive_fresh_pair", "check_sync_card"} else []
        assert call[2:] == expected
        assert module.CHECKS[name]["serial"] is True
