from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
SYNC = (ROOT / "static/js/client/sync.js").read_text()


def test_files_adopts_device_local_synced_folders_without_requesting_a_signature():
    body = APP[APP.index("function _adoptSyncPairs()"):APP.index("async function _ensureSyncPairs()")]
    assert "S.folders && S.folders()" in body
    assert "S.acct && S.acct()" in body
    assert "accountFolders" not in body
    assert "never hide a folder mapped on this very device" in body


def test_files_adopts_cached_folders_before_drawing_the_explorer():
    body = APP[APP.index("async function renderPublicFiles("):]
    assert body.index("_adoptSyncPairs();") < body.index("pane.innerHTML='<div class=\"spinner\"")


def test_background_account_folder_result_repaints_an_open_files_view():
    assert "dispatchEvent(new CustomEvent('pc:sync-folders'))" in SYNC
    listener = APP[APP.index("window.addEventListener('pc:sync-folders'"):][:400]
    assert "_adoptSyncPairs()" in listener
    assert "VIEW==='blossom'" in listener
    assert "renderBlossom()" in listener


def test_local_folder_without_remote_count_does_not_claim_zero_files():
    synced = APP[APP.index("function _fxSyncedHTML()"):APP.index("function _syncManifest(")]
    assert "Number.isFinite(f.n)" in synced
    assert "synced on this device" in synced


def test_files_home_does_not_render_an_unknown_sync_count_as_null_files():
    home = APP[APP.index("function _renderDriveHome("):APP.index("async function _renderHostRoot(")]
    assert "Number.isFinite(f.n)" in home
    assert "synced on this device" in home
    assert "tile('🔄', f.key, f.n" not in home
