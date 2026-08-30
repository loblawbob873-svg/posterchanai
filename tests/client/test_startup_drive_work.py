from pathlib import Path


APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text()


def _body(start, end):
    a = APP.index(start)
    return APP[a:APP.index(end, a)]


def test_files_pull_shares_one_inflight_request():
    """Two consumers asking for the drive together must share signer, fetch and decrypt work."""
    pull = _body("    async pull(){", "    async _pull(){")
    assert "if(this._pullP) return this._pullP" in pull
    assert "this._pullP = this._pull()" in pull
    assert "return await this._pullP" in pull
    assert "this._pullP = null" in pull
    assert "_pullP:null" in APP


def test_music_first_open_has_one_drive_read_for_repaint_and_sweep():
    music = _body("  function renderMusicApp(){", "  function _musicPhoneSettings")
    assert music.count("const indexReady = FilesIdx.ensure()") == 1
    assert "indexReady.then(ok=>ok ? MusicOffline.sweep() : 0)" in music
    assert "indexReady.then(ok=>{ if(ok" in music
    assert "FilesIdx.pull()" not in music


def test_draft_sync_does_not_take_the_critical_startup_signer_slot():
    start = _body("  function startApp(){", "  function renderMe()")
    before_hydration = start[:start.index("const hydrateUser = ()=>")]
    hydration = start[start.index("const hydrateUser = ()=>"):]
    assert "Drafts.pull()" not in before_hydration
    assert "setTimeout(()=>{ try{ Drafts.pull(); }catch(_){} }, 1200)" in hydration


def test_files_open_stays_cache_first_without_an_automatic_signer_prompt():
    files = _body("  async function renderPublicFiles(pane){", "  function _renderFilesGrid")
    assert "FilesIdx.loadLocal()" in files
    assert "FilesIdx.ensure()" not in files
    assert "FilesIdx.pull()" not in files
