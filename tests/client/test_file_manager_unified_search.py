from pathlib import Path
from tests.client_source import client_source


ROOT = Path(__file__).resolve().parents[2]
APP = client_source()
HOST = (ROOT / "static/js/client/hostfiles.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_search_aggregates_all_three_file_sources():
    assert "async function _renderFilesEverywhere" in APP
    assert "pcHost.search(q, {limit:200})" in APP
    assert "_syncManifest(pair.key)" in APP
    assert "for(const b of (drive || []))" in APP
    assert "Searching Blossom, Synced Folders and My Computer" in APP
    assert "if(_S._filesQ.trim()) return _renderFilesEverywhere(pane)" in APP


def test_search_hits_route_back_to_their_real_source():
    assert "r.source==='blossom'" in APP
    assert "r.source==='synced'" in APP
    assert "H.enter(r.dir?p:(H.parentPath(p)||p))" in APP
    assert "_filesQ=''" in APP


def test_computer_search_hits_use_cross_platform_parent_navigation():
    """Root-level Unix and Windows files must route to `/` and `C:\\`, not to the file or `C:`."""
    click = APP[APP.index("$$('.fx-search-hit',results)"):]
    click = click[:click.index("\n  }")]
    assert "H.parentPath(p)" in click
    assert "lastIndexOf" not in click
    assert "slice(0,cut)" not in click
    assert "function parentPath(p)" in HOST


def test_every_source_has_explicit_select_all_and_none():
    assert 'id="bl-selall"' in APP and 'id="bl-selnone"' in APP
    assert 'id="ss-all"' in APP and 'id="ss-none"' in APP
    assert 'class="btn btn-ghost small hf-all"' in HOST
    assert 'class="btn btn-ghost small hf-none"' in HOST


def test_synced_select_all_uses_real_subtree_paths_and_announces_toggle():
    synced = APP[APP.index("const _ssAll ="):APP.index("const trashbar", APP.index("const _ssAll ="))]
    assert "fileItems.every(it => _syncSel.has(it.path))" in synced
    assert "(_syncPath?_syncPath+'/':'')+it.name" not in synced
    assert 'aria-pressed="${_ssAll?' in synced
    assert "syncAllLabel(_ssAll, fileItems.length)" in synced
    label = APP[APP.index("function syncAllLabel("):APP.index("function paintSyncBar(")]
    assert "Deselect all" in label and "Select all" in label


def test_selecting_last_synced_checkbox_refreshes_select_all_state():
    click = APP[APP.index("$$('.syncbox', grid)"):
                APP.index("/* selmode follows", APP.index("$$('.syncbox', grid)"))]
    assert "paintSyncBar(grid, fileItems)" in click
    paint = APP[APP.index("function paintSyncBar("):]
    paint = paint[:paint.index("\n  }\n")]
    assert "fileItems.every(it => _syncSel.has(it.path))" in paint
    assert "a.setAttribute('aria-pressed'" in paint and "syncAllLabel(every" in paint
    assert "classList.toggle('has-sel'" in paint, "the bar must show its verbs when a checkbox makes a selection"


def test_details_view_has_a_real_date_column_and_cell():
    assert "['modified','Date created']" in APP
    assert 'class="fx-mod"' in APP


def test_unified_results_obey_the_shared_sort_control():
    search = APP[APP.index("async function _renderFilesEverywhere"):]
    search = search[:search.index("\n  async function renderPublicFiles")]
    assert "const resultCmp = _fxCompare(_fxSearchKey)" in search
    assert "rows.sort((a,b) => (!!a.dir !== !!b.dir)" in search
    assert ".localeCompare(String(b.name" not in search


def test_unified_sort_normalizes_dates_and_types_from_every_source():
    key = APP[APP.index("function _fxSearchKey"):]
    key = key[:key.index("\n  function _fxBytes")]
    assert "r.created || r.modified || r.mtime" in key
    assert "r.name || r.path" in key
    assert "r.mime" in key


def test_unified_results_do_not_overflow_a_phone():
    assert ".fx-search-hit{display:grid" in CSS
    assert ".fx-search-path,.fx-search-date{display:none}" in CSS


def test_clicking_a_computer_file_result_opens_it():
    """RUNS the shipped search and clicks the hits: a FILE opens (Preview/chooser), a folder browses."""
    import shutil, subprocess
    import pytest
    if not shutil.which('node'):
        pytest.skip('node is required')
    runtime = Path(__file__).with_name('unified_search_open_runtime.mjs')
    r = subprocess.run(['node', str(runtime)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_synced_select_bar_is_one_row_and_only_what_can_be_pressed():
    """2026-10-22, mobile: "Select all, select none, move to buttons and delete on every device, looks ugly and
    unaligned, wastes space" -- five controls, two disabled, wrapping over three lines on a phone."""
    css = (Path(__file__).resolve().parents[2] / "static/css/client.css").read_text()
    assert ".sync-selbar{display:flex;align-items:center;gap:6px;flex-wrap:nowrap" in css
    assert ".sync-selbar .ssb-sel{display:none}" in css and ".sync-selbar.has-sel .ssb-sel{display:inline-flex}" in css
    phone = css[css.index("@media (max-width:640px){\n  .sync-selbar"):]
    phone = phone[:phone.index("\n}")]
    assert ".sync-selbar .ssb-l{display:none}" in phone and "width:40px" in phone
    bar = APP[APP.index("const selbar = (canEdit && !_inTrash)"):APP.index("const trashbar")]
    for verb in ('id="ss-move"', 'id="ss-del"', 'id="ss-none"'):
        tag = bar[bar.rindex("<button", 0, bar.index(verb)):bar.index(">", bar.index(verb))]
        assert "ssb-sel" in tag and "aria-label=" in tag, verb      # hidden until a selection; named when icon-only
    assert "\\u2610" not in bar and "\\u2611" not in bar and "style=" not in bar
