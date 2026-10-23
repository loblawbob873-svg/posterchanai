"""Blossom Drive must paint from local state without waiting on a phone signer."""
from pathlib import Path
from tests.client_source import client_source, module_path


APP = client_source()


def test_drive_render_never_runs_an_upload_capability_probe():
    render = APP[APP.index("async function renderPublicFiles(pane){"):]
    render = render[:render.index("\n  }\n") + 4]   # its own closing brace: _vodNameMap stayed in app.js
    # The upload controls are drawn unconditionally (the never-reachable "Upload access needed" panel and its
    # `canUp` switch are gone, 2026-10-22): a refused upload asks for access when it happens, never a probe first.
    assert 'id="bl-drop"' in render and "canUp" not in render
    assert "await blossomCanUpload()" not in render
    assert "blossomCanUpload().then" not in render


def test_drive_index_reuses_the_shared_self_proof():
    # FilesIdx stayed in app.js; the Explorer that followed it up to renderPublicFiles moved to
    # files.js. The region is both halves, so it still covers exactly what it covered before.
    # FilesIdx is the whole of filesindex.js, and _vodNameMap (the old end of the region) stayed in
    # app.js — reading across the concatenation to it would sweep in the modules in between.
    fi = module_path("filesindex.js").read_text(encoding="utf-8")
    fx = APP.index("function _fxDetailsRow(", APP.index("window.PCFilesFactory = function(dep){"))
    drive = (fi[fi.index("const FilesIdx = {"):]
             + APP[fx:APP.index("async function renderPublicFiles(pane){", fx)])
    assert drive.count("await selfProof()") >= 3
    assert "sign(27235,'files-index'" not in drive
    assert "auth:btoa(JSON.stringify(auth))" not in drive


def test_blob_listing_cannot_leave_an_infinite_spinner():
    render = APP[APP.index("async function renderPublicFiles(pane){"):]
    render = render[:render.index("\n  }\n") + 4]   # its own closing brace: _vodNameMap stayed in app.js
    assert "Array.isArray(_S._filesGridList)" in render
    # The 12 s ceiling lives with the DOWNLOAD (`_listGet`, shared by every repaint of one open), not with
    # whichever repaint asked first -- test_files_list_downloads_once_per_open_full_app.py.
    assert "_listGet(server" in render
    get = APP[APP.index("function _listGet(server, pk){"):]
    get = get[:get.index("\n  }\n") + 4]
    assert "new AbortController()" in get and "12000" in get
    assert 'id="bl-list-retry"' in render


def test_large_drive_index_is_not_reparsed_on_every_blossom_repaint():
    at = APP.index("let _filesRenderLoadedKey=")
    render = APP[at:APP.index("\n  }\n", APP.index("async function renderPublicFiles(pane){", at)) + 4]
    assert "_filesRenderLoadedKey!==renderKey" in render
    assert "FilesIdx.loadLocal()" in render


def test_drive_home_reuses_folder_counts_until_index_changes():
    home = APP[APP.index("let _fxCountsRev="):APP.index("function _renderDriveHome(pane){")]
    assert "_fxCountsRev===FilesIdx._rev" in home
    assert "_fxCountsRev=FilesIdx._rev" in home
