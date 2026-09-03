"""Taskbar status panels are real, adequately sized desktop surfaces."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def test_nostr_connectivity_uses_a_native_popup_above_app_windows():
    body = OS.split("  function toggleNet(force){", 1)[1].split("  // Arrival toasts", 1)[0]
    assert "pcPopup.toggle('net'" in body
    assert body.index("pcPopup.toggle('net'") < body.index("root.appendChild(panel)")
    assert "renderNetPopup" in OS
    restore = OS.split("if(k === 'start') renderStartPopup();", 1)[1][:500]
    assert "else if(k === 'net') renderNetPopup();" in restore


def test_notification_popup_uses_nearly_the_whole_work_area():
    body = OS.split("  function toggleNoti(force){", 1)[1].split("  /* THE NOTIFICATION CENTRE", 1)[0]
    assert "vhL() - 56" in body
    assert "Math.max(420" in body
    assert "pcPopup.toggle('noti'" in body


def test_popup_window_can_accept_the_requested_tall_sizes():
    popup = MAIN.split("async function openPopupWindow(", 1)[1].split("async function placePopupWindow", 1)[0]
    assert "height: num(r.height, 160, 2200" in popup


def test_connectivity_settings_action_routes_back_to_the_shell():
    net = OS.split("function paintNet(){", 1)[1].split("/* ONE click-away handler", 1)[0]
    assert "pcPopup.pick('settings')" in net
