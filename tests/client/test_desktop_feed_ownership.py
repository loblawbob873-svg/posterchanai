from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text()


def test_async_painters_require_the_focused_live_feed_owner():
    start = OS.index("function ownsFeedView")
    end = OS.index("function routeView", start)
    helper = OS[start:end]
    assert "realFeed.parentElement===x.body" in helper
    assert "classList.contains('focused')" in helper
    assert "!w.noFeed" in helper
    assert "w.appView||w.view" in helper
    assert "ownsFeedView" in OS[OS.index("window.PCOS ="):]
