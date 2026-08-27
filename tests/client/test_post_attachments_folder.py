"""Every attachment created by a social composer belongs in Files → Posts."""
from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


def _composer():
    return APP[APP.index("function compose("):APP.index("// ---------- Blossom uploads")]


def test_every_direct_composer_upload_files_attachments_under_posts():
    composer = _composer()
    direct = [line for line in composer.splitlines()
              if "uploadBlob(files[i]" in line]
    assert len(direct) >= 4, "shared, paste, picker, and drop routes must remain covered"
    assert all("{folder:'Posts'}" in line for line in direct), direct


def test_article_and_listing_media_are_also_social_post_files():
    article = APP[APP.index("function renderArticleEditor("):APP.index("async function deleteArticle(")]
    assert "uploadBlob(f,{folder:'Posts'})" in article
    assert "uploadBlob(files[i],{folder:'Posts'})" in article

    listing = APP[APP.index("function renderListingEditor("):APP.index("async function publishListing(")]
    assert "uploadBlob(files[i],{folder:'Posts'})" in listing


def test_old_owned_post_media_is_recovered_into_posts_without_moving_named_files():
    repair = APP[APP.index("function _backfillPostFolder("):
                 APP.index("// ---------- Files: multi-select")]
    assert "authors:[ME.pubkey]" in repair
    assert "kinds:[1,20,30023,30402,34235,34236]" in repair
    assert "have.has(m[1].toLowerCase())" in repair
    assert "!FilesIdx.meta(sha)" in repair
    assert "folder:'Posts'" in repair
    assert "FilesIdx.beginBatch()" in repair and "FilesIdx.endBatch()" in repair
    assert "_backfillPostFolder(list);" in APP
