"""The Android Files view has exactly one vertical touch scroller."""

from pathlib import Path


CSS = (Path(__file__).parents[2] / "static/css/client.css").read_text(encoding="utf-8")


def test_phone_files_pane_owns_vertical_touch_scrolling():
    start = CSS.index("@media(max-width:820px){", CSS.index(".feed.feed-files"))
    block = CSS[start:CSS.index("/* ---------- Chess hub", start)]
    assert ".feed-files #files-pane" in block
    assert "overflow-y:auto" in block
    assert "-webkit-overflow-scrolling:touch" in block
    assert "touch-action:pan-y" in block
    assert ".feed-files #files-pane>.fx-explorer{flex:0 0 auto;overflow:visible}" in block
    assert ".feed-files #files-pane>.fx-explorer>.fx-main{overflow:visible}" in block
