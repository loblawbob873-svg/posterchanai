from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "templates/client.html").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def test_blossom_route_is_presented_as_file_manager():
    assert 'data-view="blossom"' in SHELL
    nav = SHELL[SHELL.index('data-view="blossom"'):][:180]
    assert '<span>File Manager</span>' in nav
    assert "{ v:'blossom', svg:ICO('flower'), label:'File Manager' }" in APP


def test_internal_route_stays_stable_for_saved_windows_and_links():
    assert "if (VIEW==='blossom') return renderBlossom()" in APP
