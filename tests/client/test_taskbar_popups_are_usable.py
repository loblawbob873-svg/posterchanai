"""Taskbar status panels are real, adequately sized desktop surfaces."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def test_nostr_connectivity_uses_a_native_popup_above_app_windows():
    # The popup call lives in `_netPopup` now — extracted so the paint flag cannot be consulted
    # before the process that owns the window is asked (see test_start_menu_is_its_own_window).
    body = (OS.split("  function _netPopup(){", 1)[1].split("\n  function toggleNet", 1)[0]
            + OS.split("  function toggleNet(force){", 1)[1].split("  // Arrival toasts", 1)[0])
    assert "pcPopup.toggle('net'" in body
    assert body.index("pcPopup.toggle('net'") < body.index("root.appendChild(panel)")
    assert "renderNetPopup" in OS
    restore = OS.split("if(k === 'start') renderStartPopup();", 1)[1][:500]
    assert "else if(k === 'net') renderNetPopup();" in restore


def test_notification_popup_uses_nearly_the_whole_work_area():
    body = (OS.split("  function _notiPopup(){", 1)[1].split("\n  function toggleNoti", 1)[0]
            + OS.split("  function toggleNoti(force){", 1)[1].split("  /* THE NOTIFICATION CENTRE", 1)[0])
    assert "vhL() - 56" in body
    assert "Math.max(420" in body
    assert "pcPopup.toggle('noti'" in body


def test_popup_window_can_accept_the_requested_tall_sizes():
    popup = MAIN.split("async function openPopupWindow(", 1)[1].split("async function placePopupWindow", 1)[0]
    assert "height: num(r.height, 160, 2200" in popup


def test_connectivity_settings_action_routes_back_to_the_shell():
    net = OS.split("function paintNet(){", 1)[1].split("/* ONE click-away handler", 1)[0]
    assert "pcPopup.pick('settings')" in net


def test_connectivity_popup_uses_shell_snapshot_not_its_cold_relay_pool():
    toggle = (OS.split("  function _netPopup(){", 1)[1].split("\n  function toggleNet", 1)[0]
              + OS.split("  function toggleNet(force){", 1)[1].split("  // Arrival toasts", 1)[0])
    render = OS.split("  function renderNetPopup(){", 1)[1].split("  function restore(){", 1)[0]
    paint = OS.split("  function paintNet(){", 1)[1].split("/* ONE click-away handler", 1)[0]
    assert "const state = netState();" in toggle
    assert "state.conns.slice(0,20)" in toggle
    # The SNAPSHOT is what this test is about; the geometry beside it moved to compositor pixels
    # (see tests/client/test_popup_geometry_is_compositor_pixels.py) and pinning that expression
    # here only made this fail for an unrelated fix.
    assert "pcPopup.toggle('net'" in toggle
    assert ", snapshot)" in toggle
    assert "URLSearchParams(window.location.search).get('pcarg')" in render
    assert "_popupNetState = s" in render
    assert "const s = _popupNetState || netState();" in paint


def test_connectivity_popup_reconnects_authoritative_shell_pool():
    paint = OS.split("  function paintNet(){", 1)[1].split("/* ONE click-away handler", 1)[0]
    actions = OS.split("else if(p.indexOf('pc:act:') === 0)", 1)[1].split("else if(p === 'pc:tasks')", 1)[0]
    assert "pcPopup.act('net-reconnect')" in paint
    assert "kind === 'net-reconnect'" in actions
    assert "api.reconnectNetwork" in actions


def test_popup_argument_limit_fits_normal_relay_snapshots():
    popup = MAIN.split("async function openPopupWindow(", 1)[1].split("async function placePopupWindow", 1)[0]
    assert ".slice(0, 8192)" in popup


def test_closing_native_connectivity_popup_clears_shell_open_latch():
    closed = OS.split("p.indexOf('pc:popup-closed:') === 0", 1)[1].split("drawBar();", 1)[0]
    assert "kind === 'net'" in closed
    assert "netOpen = false" in closed
