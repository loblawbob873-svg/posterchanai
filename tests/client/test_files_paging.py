from pathlib import Path
from tests.client_source import client_source


APP = client_source()


def test_large_blossom_folders_say_how_many_files_are_visible():
    assert 'class="files-page-count"' in APP
    assert 'Showing ${_shown.length} of ${inFolder.length}' in APP


def test_large_blossom_folders_progressively_load_while_scrolling():
    assert "const observer=new IntersectionObserver" in APP
    # the element that actually SCROLLS (on a phone .fx-main does not, and the old root loaded every page with
    # nobody scrolling -- test_files_all_does_not_load_itself_on_a_phone_full_app.py drives that for real)
    assert "root:_scrollRoot(mb)" in APP
    assert "root:mb.closest('.fx-main')" not in APP
    assert "if(mb.isConnected)mb.click()" in APP
    assert "_filesShown+=_FILES_PAGE" in APP
