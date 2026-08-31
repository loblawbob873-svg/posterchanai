from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates/client.html").read_text()
APP = (ROOT / "static/js/client/app.js").read_text()


def test_large_view_only_modules_are_not_on_every_page_load():
    for name in ("meme.js", "markets.js", "stats.js"):
        assert f'<script src="/static/js/client/{name}' not in HTML
        assert f"renderModuleView('{name[:-3]}','{name}'" in APP


def test_meme_entry_points_wait_for_the_lazy_module():
    assert APP.count("_withModule('meme.js','PCMeme').then") >= 2
