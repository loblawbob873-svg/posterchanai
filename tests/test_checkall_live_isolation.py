"""The release suite must not manufacture relay failures by load-testing its own live checks."""
from contextlib import nullcontext
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
@pytest.mark.parametrize("explicit_selection", [False, True])
def test_real_service_checks_require_explicit_live_mode(tmp_path, monkeypatch, live, explicit_selection):
    module = _checkall()
    monkeypatch.setattr(module, "_runner_lock", nullcontext)
    supplied_url = {
        "check_drive_fresh_pair", "check_sync_card", "check_nostrconnect_remote_signer",
        "check_files_home_navigates", "check_composer_survives_a_desktop_click",
        "check_os_window_controls_are_reachable",
    }
    installed_renderer = {
        "check_installed_desktop_account", "check_installed_admin_prune_preview",
        "check_installed_native_files", "check_installed_system_settings",
        "check_installed_code", "check_installed_code_focus", "check_installed_native_focus",
        "check_installed_native_snap", "check_installed_native_handoff",
    }
    existing_endpoint = {
        "check_concord_live_invite", "check_signer_transport", "check_websearch_rate",
        "check_nip46_signer", "check_nip46_reconnect", "check_nip46_bulk_lane",
    } | installed_renderer
    names = supplied_url | existing_endpoint
    # Positive controls: package extraction and isolated browser fixtures still run offline.
    offline = {"check_installed_code_package_release", "check_installed_document_apps_release",
               "check_installed_wm_release", "check_your_files_are_reachable"}
    selected = names | offline
    jobs = [job for job in module.discover() if job["name"] in selected]
    assert len(jobs) == len(selected)
    calls = []
    monkeypatch.setattr(module, "discover", lambda: jobs)
    monkeypatch.setattr(module, "SUITES", [])
    monkeypatch.setattr(module, "have_chrome", lambda: "/test/chrome")
    monkeypatch.setattr(module, "have_node", lambda: "/test/node")
    def capture(argv, cwd, env, timeout, log, **kwargs):
        calls.append((argv, env))
        return 0, "OK"
    monkeypatch.setattr(module, "_captured", capture)
    argv = ["checkall", "--tmp", str(tmp_path), "--jobs", "1"]
    if explicit_selection:
        argv += ["--only", ",".join(sorted(selected))]
    if live:
        argv += ["--live", "https://test.invalid"]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    called = {Path(call[1]).stem for call, env in calls}
    assert called == (selected if live else offline), "live service selection or offline coverage changed"
    assert len(calls) == len(called), "a selected check ran more than once"
    for call, env in calls:
        name = Path(call[1]).stem
        expected = ["https://test.invalid"] if name in supplied_url else []
        assert call[2:] == expected
        if name in names:
            assert module.CHECKS[name]["serial"] is True
        if name in installed_renderer:
            assert env["PC_CHECK_PORT"] == "9223"
