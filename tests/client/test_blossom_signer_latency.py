"""Blossom Drive must paint from local state without waiting on a phone signer."""
from pathlib import Path


APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text()


def test_upload_capability_probe_is_cached_and_not_awaited_by_drive_render():
    fn = APP[APP.index("async function blossomCanUpload(){"):]
    fn = fn[:fn.index("\n  }") + 4]
    assert "_blossomUploadP" in fn
    assert "300000" in fn
    render = APP[APP.index("async function renderPublicFiles(pane){"):]
    render = render[:render.index("\n  let _vodNameMap")]
    assert "const canUp=_blossomUploadOK!==false" in render
    assert "await blossomCanUpload()" not in render
    assert "blossomCanUpload().then" in render


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
