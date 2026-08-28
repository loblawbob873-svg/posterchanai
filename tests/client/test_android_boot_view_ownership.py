from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def test_public_view_request_owns_destination_before_async_boot_landing():
    """The JS API is live before async Android boot finishes.

    A native launch/module request made in that window must not be overwritten by the ordinary
    Social landing. This is the source-level half of AppViewsLaunchSmokeTest's real Chromium gate;
    the latter opens AI immediately and asserts it is still AI after deferred work has settled.
    """
    assert "let _publicViewRequests = 0;" in APP
    wrapper = APP[APP.index("function requestView(v, quiet)"):
                  APP.index("function _rememberTlScroll", APP.index("function requestView(v, quiet)"))]
    assert "_publicViewRequests++;" in wrapper
    assert "return switchView(v, quiet);" in wrapper
    assert "switchView:requestView" in APP
    assert "if(!_osHome && !_publicViewRequests){ switchView(_startView());" in APP


def test_internal_boot_routing_does_not_claim_public_view_ownership():
    """Only the public bridge owns a destination; internal config gating still gets a landing."""
    gate = APP[APP.index("function applyInstanceGating()"):
               APP.index("/* ===== HIDING ROWS", APP.index("function applyInstanceGating()"))]
    assert "requestView(" not in gate
    assert "switchView(_startTimeline())" in gate
