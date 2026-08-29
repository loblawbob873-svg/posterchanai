from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
SPRITE = (ROOT / "static/js/client/sprite.js").read_text()


def test_blossom_file_icons_do_not_depend_on_platform_emoji_fonts():
    block = APP[APP.index("function _fxFileGlyph"):APP.index("function _fxColsHTML")]
    for glyph in ("🖼", "🎬", "🎵", "📕", "📦", "📄", "📎"):
        assert glyph not in block
    assert '<svg class="fx-file-ic fx-file-' in block


def test_every_file_kind_maps_to_a_sprite_that_ships_in_all_bundles():
    mapping = {
        "image": "i-image", "video": "i-film", "audio": "i-music",
        "pdf": "i-article", "archive": "i-folder", "document": "i-text",
        "folder": "i-folder", "file": "i-paperclip",
    }
    glyph = APP[APP.index("function _fxFileGlyph"):APP.index("function _fxIcon")]
    for kind, symbol in mapping.items():
        assert f"{kind}:'{symbol.removeprefix('i-')}'" in glyph
        assert f'id="{symbol}"' in SPRITE


def test_file_icons_keep_the_clean_file_manager_layout_but_are_visible():
    assert ".file-card{border:1px solid transparent" in CSS
    assert ".file-icon{display:flex" in CSS and "background:none" in CSS
    assert ".fx-file-ic{width:58px;height:58px" in CSS
    for kind in ("image", "video", "audio", "pdf", "archive", "document", "folder", "file"):
        assert f".fx-file-{kind}" in CSS


def test_thumbnail_failures_fall_back_to_the_same_deterministic_icons():
    block = APP[APP.index("function _bindThumbFallback"):APP.index("function blobThumb")]
    assert "_fxFileGlyph(kind)" in block
    assert "swap(im,'video'" in block
    assert "swap(im,'file'" in block


def test_restored_mime_and_supported_extension_variants_keep_their_icons():
    block = APP[APP.index("function _fxIcon"):APP.index("function _fxColsHTML")]
    assert "String(type || '').toLowerCase()" in block
    for ext in ("jfif", "heif", "tiff", "ogv", "3gp", "m4b", "aiff", "mka"):
        assert ext in block


def test_every_type_column_document_and_archive_extension_has_a_matching_icon():
    """A restored file can have no MIME, so the extension paths must not drift apart."""
    kinds = APP[APP.index("const _FX_KINDS"):APP.index("function _fxType")]
    icons = APP[APP.index("function _fxIcon"):APP.index("function _fxColsHTML")]
    for ext in ("rtf", "doc", "docx", "odt", "xls", "xlsx", "ods",
                "ppt", "pptx", "odp", "epub", "bz2"):
        assert ext in kinds
        assert ext in icons
