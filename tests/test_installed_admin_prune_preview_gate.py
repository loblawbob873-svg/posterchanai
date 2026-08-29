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


def test_installed_gate_returns_to_the_same_settings_owner_after_preview():
    assert "RETURN_SETTINGS" in GATE
    assert "PCOS.routeView('settings')" in GATE
    assert "await __PC.switchView('settings')" in GATE
    assert '"hostHidden": True' in GATE
    os_js = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    assert "view: w.view, appView: w.appView || w.view" in os_js
