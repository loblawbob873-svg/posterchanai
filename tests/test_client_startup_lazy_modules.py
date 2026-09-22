from pathlib import Path

from tests.client_source import client_source


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates/client.html").read_text()
APP = client_source()   # the Meme Builder entry points live in menus.js and app.js now


def test_large_view_only_modules_are_not_on_every_page_load():
    for name in ("meme.js", "markets.js", "stats.js"):
        assert f'<script src="/static/js/client/{name}' not in HTML
        assert f"renderModuleView('{name[:-3]}','{name}'" in APP


def test_meme_entry_points_wait_for_the_lazy_module():
    assert APP.count("_withModule('meme.js','PCMeme').then") >= 2
