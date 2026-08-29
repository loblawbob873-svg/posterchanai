"""The release suite must not manufacture relay failures by load-testing its own live checks."""
import importlib.util
from pathlib import Path


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
