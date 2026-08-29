from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
HOST = (ROOT / "static/js/client/hostfiles.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_search_aggregates_all_three_file_sources():
    assert "async function _renderFilesEverywhere" in APP
    assert "pcHost.search(q, {limit:200})" in APP
    assert "_syncManifest(pair.key)" in APP
    assert "for(const b of (drive || []))" in APP
    assert "Searching Blossom, Synced Folders and My Computer" in APP
    assert "if(_filesQ.trim()) return _renderFilesEverywhere(pane)" in APP


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
    assert "Deselect all" in synced


def test_selecting_last_synced_checkbox_refreshes_select_all_state():
    click = APP[APP.index("$$('.syncbox', grid)"):
                APP.index("/* selmode follows", APP.index("$$('.syncbox', grid)"))]
    assert "fileItems.every(it => _syncSel.has(it.path))" in click
    assert "a2.setAttribute('aria-pressed'" in click
    assert "a2.textContent" in click


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
