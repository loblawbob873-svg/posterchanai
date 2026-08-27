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
