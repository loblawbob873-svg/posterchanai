from pathlib import Path


ROOT = Path(__file__).parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def test_article_body_images_are_bounded_without_being_cropped():
    start = CSS.index(".article-view .av-body img{")
    rule = CSS[start:CSS.index("}", start)]
    assert "max-height:min(60vh,560px)" in rule
    assert "max-width:100%" in rule
    assert "object-fit:contain" in rule
    assert "width:auto" in rule


def test_bounded_article_images_still_open_at_full_size():
    article = APP[APP.index("function openArticle("):APP.index("function articleAddr(")]
    assert "feed.querySelectorAll('.markdown img')" in article
    assert "openLightbox(im.currentSrc||im.src)" in article
