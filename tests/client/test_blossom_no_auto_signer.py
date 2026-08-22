from pathlib import Path


SRC = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text()


def test_opening_blossom_does_not_automatically_request_a_signature():
    assert "blossomCanUpload().then" not in SRC
    assert "FilesIdx.ensure().then(ok=>" not in SRC
    assert "_ensureSyncPairs().then(changed=>" not in SRC
    assert "data-load-sync-folders" in SRC
    assert "fx-refresh" in SRC


def test_failed_drive_pull_does_not_repaint_and_retry():
    assert "if(ok && VIEW==='blossom') renderBlossom()" not in SRC
