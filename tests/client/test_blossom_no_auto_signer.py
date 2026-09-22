from pathlib import Path
from tests.client_source import client_source


SRC = client_source()


def test_opening_blossom_does_not_automatically_request_a_signature():
    assert "blossomCanUpload().then" not in SRC
    assert "FilesIdx.ensure().then(ok=>" not in SRC
    assert "_ensureSyncPairs().then(changed=>" not in SRC
    assert "data-load-sync-folders" in SRC
    assert "fx-refresh" in SRC


def test_failed_drive_pull_does_not_repaint_and_retry():
    assert "if(ok && VIEW==='blossom') renderBlossom()" not in SRC
