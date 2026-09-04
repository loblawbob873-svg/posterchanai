from pathlib import Path


def test_notification_flyout_has_compositor_action_open_and_close_routes():
    os_js = (Path(__file__).resolve().parents[1] / "static/js/client/os.js").read_text()
    assert "p === 'pc:notifications') toggleNoti(true)" in os_js
    close = os_js[os_js.index("p === 'pc:notifications:close'") :]
    close = close[:close.index("else if(p === 'pc:shot')")]
    assert "toggleNoti(false)" in close
    assert "pcPopup.close()" in close
