from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_file_manager_uses_collapsible_blossom_and_computer_roots():
    assert 'data-fxtoggle="blossom"' in APP
    assert 'data-fxtoggle="computer"' in APP
    assert '<b>Blossom</b>' in APP
    assert '<b>My Computer</b>' in APP
    assert "pc.files.tree." in APP
    assert "aria-expanded" in APP


def test_tree_is_real_sidebar_hierarchy_not_unstyled_text():
    for selector in (".fx-tree", ".fx-tree-node", ".fx-tree-head", ".fx-tree-children"):
        assert selector in CSS


def test_top_source_tabs_are_removed_in_favor_of_sidebar_navigation():
    render = APP[APP.index("async function renderBlossom()"):
                 APP.index("// Admin tab:")]
    assert 'class="files-tabs"' not in render
    assert 'class="ftab' not in render
    assert "_fxSideHTML()" in render
    assert 'data-files-mode="ai"' in APP
    assert 'data-files-mode="admin"' in APP


def test_blossom_picker_uses_the_same_collapsible_file_manager_tree():
    picker = APP[APP.index("function blossomPicker(ta, onPick, opts={})"):
                 APP.index("// ---------- Pics:")]
    assert 'class="bp-explorer"' in picker
    assert 'aria-label="Blossom folders"' in picker
    assert 'id="bp-tree-toggle"' in picker
    assert '<b>Blossom</b>' in picker
    assert 'data-folder=' in picker
    assert 'bp-folder-sel' not in picker
    assert "toggle.setAttribute('aria-expanded'" in picker


def test_file_picker_has_bounded_desktop_and_mobile_layouts():
    for selector in (".bp-file-picker", ".bp-explorer", ".bp-explorer>.bp-folders",
                     ".bp-explorer>.files-grid"):
        assert selector in CSS
    assert "height:min(720px,calc(100vh - 28px))" in CSS
    assert "grid-template-columns:132px minmax(0,1fr)" in CSS
