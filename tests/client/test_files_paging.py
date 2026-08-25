from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


def test_large_blossom_folders_say_how_many_files_are_visible():
    assert 'class="files-page-count"' in APP
    assert 'Showing ${_shown.length} of ${inFolder.length}' in APP


def test_large_blossom_folders_progressively_load_while_scrolling():
    assert "const observer=new IntersectionObserver" in APP
    assert "root:mb.closest('.fx-main')" in APP
    assert "if(mb.isConnected)mb.click()" in APP
    assert "_filesShown+=_FILES_PAGE" in APP
