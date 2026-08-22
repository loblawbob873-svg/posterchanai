from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(errors="replace")
CSS = (ROOT / "static/css/client.css").read_text()


def test_new_posts_button_follows_timeline_into_desktop_window():
    """The shell is behind .osw windows, so a pill left on .main exists but cannot be seen."""
    block = APP[APP.index("function _newPostsPill(){"):APP.index("function _placePill(p){")]
    assert "feed.closest('.osw-body')" in block
    assert "host.appendChild(p)" in block
    assert ".osw-body:has(> .new-posts-pill){position:relative}" in CSS


def test_click_still_releases_posts_and_returns_to_top():
    block = APP[APP.index("function _newPostsPill(){"):APP.index("function _placePill(p){")]
    assert "feed.scrollTop=0" in block
    assert "_flushPending()" in block
