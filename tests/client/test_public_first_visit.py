from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text()
APP = (ROOT / "static/js/client/app.js").read_text()


def test_public_browser_does_not_default_to_desktop_mode():
    start = OS.index("function restore(){")
    body = OS[start:OS.index("let _superClean", start)]
    assert "PCOSShell.available()){ enter(); return; }" in body
    assert "settings().get(KEY, false) && fits()" in body
    assert "settings().get(KEY, true) && fits()" not in body


def test_signed_out_classic_landing_explains_the_next_actions():
    start = APP.index("function _guestCardHtml(){")
    body = APP[start:APP.index("function _timelineHeaderHtml", start)]
    assert "PosterChan AI" in body
    assert 'id="guest-signup"' in body
    assert 'id="guest-login2"' in body
