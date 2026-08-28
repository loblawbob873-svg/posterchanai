from pathlib import Path


CSS = (Path(__file__).parents[1] / "static/css/client.css").read_text()


def test_mobile_files_use_compact_square_tiles():
    """The phone Files view must not regress to large landscape image cards."""
    mobile = CSS[CSS.index("@media(max-width:700px)") : CSS.index("@media(max-width:359px)")]
    assert "repeat(auto-fill,minmax(92px,1fr))" in mobile
    assert ".files-grid:not(.details) .file-card>img" in mobile
    assert "aspect-ratio:1" in mobile
    assert "object-fit:cover" in mobile


def test_mobile_details_view_remains_a_file_list():
    """Square artwork applies to icon view only, never the sortable details table."""
    mobile = CSS[CSS.index("@media(max-width:700px)") : CSS.index("@media(max-width:359px)")]
    assert ".files-grid:not(.details)" in mobile
    assert ".files-grid.details .file-card>img" not in mobile
