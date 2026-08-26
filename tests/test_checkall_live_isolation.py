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
