"""Blossom Drive must paint from local state without waiting on a phone signer."""
from pathlib import Path


APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text()


def test_drive_render_never_runs_an_upload_capability_probe():
    render = APP[APP.index("async function renderPublicFiles(pane){"):]
    render = render[:render.index("\n  let _vodNameMap")]
    assert "const canUp=true" in render
    assert "await blossomCanUpload()" not in render
    assert "blossomCanUpload().then" not in render


def test_drive_index_reuses_the_shared_self_proof():
    drive = APP[APP.index("const FilesIdx = {"):APP.index("async function renderPublicFiles(pane){")]
    assert drive.count("await selfProof()") >= 3
    assert "sign(27235,'files-index'" not in drive
    assert "auth:btoa(JSON.stringify(auth))" not in drive


def test_blob_listing_cannot_leave_an_infinite_spinner():
    render = APP[APP.index("async function renderPublicFiles(pane){"):]
    render = render[:render.index("\n  let _vodNameMap")]
    assert "Array.isArray(_filesGridList)" in render
    assert "new AbortController()" in render
    assert "12000" in render
    assert 'id="bl-list-retry"' in render


def test_large_drive_index_is_not_reparsed_on_every_blossom_repaint():
    render = APP[APP.index("let _filesRenderLoadedKey="):APP.index("\n  let _vodNameMap")]
    assert "_filesRenderLoadedKey!==renderKey" in render
    assert "FilesIdx.loadLocal()" in render


def test_drive_home_reuses_folder_counts_until_index_changes():
    home = APP[APP.index("let _fxCountsRev="):APP.index("function _renderDriveHome(pane){")]
    assert "_fxCountsRev===FilesIdx._rev" in home
    assert "_fxCountsRev=FilesIdx._rev" in home
