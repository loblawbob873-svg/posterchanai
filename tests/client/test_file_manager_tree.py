from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_file_manager_uses_collapsible_blossom_and_computer_roots():
    assert 'data-fxtoggle="blossom"' in APP
    assert 'data-fxtoggle="computer"' in APP
    assert 'data-fxtoggle="synced"' in APP
    assert '<b>Files</b>' in APP
    assert '<b>My Computer</b>' in APP
    assert '<b>Synced Folders</b>' in APP
    assert "pc.files.tree." in APP
    assert "aria-expanded" in APP


def test_tree_is_real_sidebar_hierarchy_not_unstyled_text():
    for selector in (".fx-tree", ".fx-tree-node", ".fx-tree-head", ".fx-tree-children"):
        assert selector in CSS


def test_mobile_uses_source_switcher_and_only_active_source_locations():
    assert 'id="fx-locations-open"' in APP
    assert 'id="fx-locations-close"' in APP
    assert ".fx-explorer.fx-locations-on>.fx-side" in CSS
    assert "width:min(94vw,390px)" in CSS
    assert "position:fixed" in CSS
    assert "100vw 0 0 100vw rgba(0,0,0,.58)" in CSS
    assert "explorer.classList.add('fx-locations-on')" in APP
    assert "explorer.classList.remove('fx-locations-on')" in APP


def test_mobile_locations_backdrop_closes_without_opening_an_underlying_file():
    start = APP.index("function _fxBindSide(")
    bind = APP[start:start + 8000]
    assert "explorer.addEventListener('click',e=>{" in bind
    assert "!explorer.classList.contains('fx-locations-on')" in bind
    assert "e.target.closest('.fx-side')" in bind
    assert "e.preventDefault();e.stopPropagation()" in bind
    assert "},true)" in bind


def test_mobile_source_heads_navigate_instead_of_repainting_the_same_drawer():
    start = APP.index("function _fxBindSide(")
    bind = APP[start:start + 7000]
    assert "if(mobile && which==='blossom')" in bind
    assert "_filesFolder=null" in bind
    assert "if(mobile && which==='computer')" in bind
    assert "_openHostFiles()" in bind
    # Synced Folders is a disclosure and must visibly toggle without destroying the open drawer.
    assert "tree.classList.toggle('hidden',!open)" in bind


def test_view_buttons_leave_the_folder_dashboard_and_show_the_selected_file_view():
    bind = APP[APP.index("function _fxBindBar("):APP.index("let _filesFolder = null")]
    assert "ClientSettings.set('filesView', b.dataset.view)" in bind
    assert "_filesFolder === null) _filesFolder = ''" in bind


def test_mobile_details_rows_collapse_actions_into_one_menu():
    assert 'class="fx-mobile-actions"' in APP
    assert 'summary aria-label="File actions"' in APP
    assert 'class="fx-more-dots"' in APP
    assert ".files-grid.details .file-card.row>.fc-acts{display:none}" in CSS
    assert ".fx-mobile-actions>.fc-acts" in CSS


def test_top_source_tabs_are_removed_in_favor_of_sidebar_navigation():
    render = APP[APP.index("async function renderBlossom()"):
                 APP.index("// Admin tab:")]
    assert 'class="files-tabs"' not in render
    assert 'class="ftab' not in render
    assert "_fxSideHTML()" in render
    assert 'data-files-mode="ai"' in APP
    assert 'data-files-mode="admin"' in APP


def test_new_folder_is_in_toolbar_beside_layout_controls_not_in_tree():
    bar = APP[APP.index("function _fxBarHTML("):
              APP.index("function _fxBindBar(")]
    side = APP[APP.index("function _fxSideHTML("):
               APP.index("function _fxHostHTML(")]
    assert 'id="bl-newfolder"' in bar
    assert bar.index('id="bl-newfolder"') < bar.index('data-view="tiles"')
    assert 'id="bl-newfolder"' not in side
    assert "if(nf) nf.onclick=_newFolderModal" in APP[APP.index("function _fxBindBar("):
                                                         APP.index("let _filesFolder = null")]


def test_blossom_picker_uses_the_same_collapsible_file_manager_tree():
    picker = APP[APP.index("function blossomPicker(ta, onPick, opts={})"):
                 APP.index("// ---------- Pics:")]
    assert 'class="bp-explorer"' in picker
    assert 'aria-label="Blossom folders"' in picker
    assert 'id="bp-tree-toggle"' in picker
    assert '<b>Files</b>' in picker
    assert 'data-folder=' in picker
    assert 'bp-folder-sel' not in picker
    assert "toggle.setAttribute('aria-expanded'" in picker


def test_file_picker_has_bounded_desktop_and_mobile_layouts():
    for selector in (".bp-file-picker", ".bp-explorer", ".bp-explorer>.bp-folders",
                     ".bp-explorer>.files-grid"):
        assert selector in CSS
    assert "height:min(720px,calc(100vh - 28px))" in CSS
    assert ".bp-explorer{display:flex;flex-direction:column;position:relative}" in CSS
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in CSS
    assert ".modal-bg.bp-picker-bg{inset:0;padding:0;place-items:stretch" in CSS
    assert "height:100dvh;margin:0;border:0;border-radius:0" in CSS
    assert "transform:translateX(-105%)" in CSS
    assert ".bp-explorer.bp-locations-on>.bp-folders{transform:translateX(0)}" in CSS
    assert ".bp-explorer .folder-bar{flex-direction:column;overflow:visible" in CSS
    assert "white-space:normal;overflow-wrap:anywhere" in CSS


def test_mobile_picker_identifies_each_file_beside_a_real_preview():
    picker = APP[APP.index("function blossomPicker(ta, onPick, opts={})"):
                 APP.index("// ---------- Pics:")]
    assert 'class="file-card bp-pick-card"' in picker
    assert 'class="fname"' in picker
    assert "_fmtBytes(b.size||0)" in picker
    assert "enc(fmtBytes(" not in picker
    assert "onPick({url, type, ext, name})" in picker
    assert ".bp-explorer .bp-pick-card .fname" in CSS


def test_picker_recovers_video_mime_from_index_or_filename_before_filtering():
    picker = APP[APP.index("function blossomPicker(ta, onPick, opts={})"):
                 APP.index("// ---------- Pics:")]
    assert "if(!b.name && m.name) b.name=m.name" in picker
    assert "b.type=mimeForName(b.name||'')||b.type||''" in picker
    assert picker.index("b.type=mimeForName") < picker.index("if(opts.filter")


def test_picker_strips_mime_parameters_before_deriving_the_extension():
    picker = APP[APP.index("function blossomPicker(ta, onPick, opts={})"):
                 APP.index("// ---------- Pics:")]
    assert "const bareType=type.replace(/;.*/, '').trim().toLowerCase()" in picker
    assert "const ext=_MIME_EXT[bareType]||''" in picker


def test_large_file_picker_results_cannot_squash_thumbnails_into_lines():
    """A bounded grid with many implicit rows must scroll, never divide its height among them."""
    assert ".bp-explorer>.files-grid{grid-auto-rows:max-content" in CSS
    assert ".bp-explorer>.files-grid>.file-card{min-width:0;min-height:96px" in CSS
    assert ".bp-explorer>.files-grid>.file-card>img," in CSS
    assert "height:96px;min-height:96px;object-fit:cover" in CSS
    assert "overscroll-behavior:contain" in CSS
