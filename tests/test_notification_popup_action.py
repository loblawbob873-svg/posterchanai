from pathlib import Path


def test_notification_flyout_has_compositor_action_open_and_close_routes():
    os_js = (Path(__file__).resolve().parents[1] / "static/js/client/os.js").read_text()
    assert "p === 'pc:notifications') toggleNoti(true)" in os_js
    assert "p === 'pc:notifications:close') toggleNoti(false)" in os_js
