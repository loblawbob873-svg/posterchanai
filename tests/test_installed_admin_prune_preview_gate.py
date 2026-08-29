from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE = (ROOT / "scripts/check_installed_admin_prune_preview.py").read_text(encoding="utf-8")
ADMIN = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text(encoding="utf-8")


def test_preview_is_a_non_submitting_button_and_the_gate_clicks_that_exact_control():
    assert 'type="button" id="relayPruneDryBtn"' in ADMIN
    assert "#relayPruneDryBtn" in GATE
    assert "dry run" in GATE


def test_installed_gate_reproduces_narrow_display_reconciliation_during_the_request():
    assert "Object.defineProperty(window,'innerWidth',{value:900" in GATE
    assert "dispatchEvent(new Event('resize'))" in GATE
    assert "Object.getOwnPropertyDescriptor(window,'innerWidth')" in GATE


def test_installed_gate_proves_desktop_route_host_and_window_ownership_survive():
    for marker in ("PCOS.isOn()", "#os-root", "os-on", "__PC.VIEW", "#admin-host", ".osw"):
        assert marker in GATE
    assert "ever_off" in GATE
    assert "frame.classList.contains('focused')" in GATE
    assert "w.view==='settings'" in GATE
    assert "ownerView" in GATE


def test_installed_gate_checks_route_host_focus_and_owner_during_the_whole_dry_run():
    start = GATE.index("for _ in range(360):")
    loop = GATE[start:GATE.index("result = await inner.eval", start)]
    for invariant in ("ever_wrong_route", "ever_lost_host", "ever_lost_focus", "ever_lost_owner"):
        assert invariant in loop
        assert f"assert not {invariant}" in GATE


def test_installed_gate_returns_to_the_same_settings_owner_after_preview():
    assert "RETURN_SETTINGS" in GATE
    assert "PCOS.routeView('settings')" in GATE
    assert "await __PC.switchView('settings')" in GATE
    assert '"hostHidden": True' in GATE
    os_js = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    assert "view: w.view, appView: w.appView || w.view" in os_js


def test_installed_gate_uses_the_guarded_throwaway_diagnostic_login():
    """Release automation must not need a personal account or duplicate the nsec safety checks."""
    assert "from check_installed_desktop_account import CDP, choose_test_page" in GATE
    assert "page = await choose_test_page()" in GATE
    helper = (ROOT / "scripts/check_installed_desktop_account.py").read_text(encoding="utf-8")
    assert 'os.environ.get("PC_INSTALLED_TEST_NSEC_FILE"' in helper
    assert "identity guard; refusing login" in helper


def test_diagnostic_login_still_requires_a_real_posterchanos_shell():
    start = GATE.index("async def parent_page():")
    body = GATE[start:GATE.index("async def admin_frame()", start)]
    assert "PCOSShell.available()" in body
    assert "if not ok:" in body
    assert "not a PosterChanOS shell" in body
