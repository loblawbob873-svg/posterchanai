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
    assert "H.enter(r.dir?p:" in APP
    assert "_filesQ=''" in APP


def test_every_source_has_explicit_select_all_and_none():
    assert 'id="bl-selall"' in APP and 'id="bl-selnone"' in APP
    assert 'id="ss-all"' in APP and 'id="ss-none"' in APP
    assert 'class="btn btn-ghost small hf-all"' in HOST
    assert 'class="btn btn-ghost small hf-none"' in HOST


def test_details_view_has_a_real_date_column_and_cell():
    assert "['modified','Date created']" in APP
    assert 'class="fx-mod"' in APP


def test_unified_results_do_not_overflow_a_phone():
    assert ".fx-search-hit{display:grid" in CSS
    assert ".fx-search-path,.fx-search-date{display:none}" in CSS

