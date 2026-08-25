from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


def test_recovery_is_explicit_and_ordinary_pull_does_not_fetch_history():
    pull = APP[APP.index("async _pull(){") : APP.index("push(){", APP.index("async _pull(){"))]
    assert "history:true" not in pull
    assert "Recover folder list…" in APP
    assert "async history()" in APP


def test_restore_invalidates_the_old_version_and_uses_the_normal_decrypting_pull():
    body = APP[APP.index("async restore(slot){") : APP.index("async pull(){", APP.index("async restore(slot){"))]
    assert "restore:Number(slot)" in body
    for flag in ("this._pullDone=false", "this._pullOk=false", "this._pullBlocked=false"):
        assert flag in body
    assert "await this.pull()" in body
    assert "if(!this._pullOk)" in body


def test_restore_is_confirmed_and_explains_that_bytes_are_untouched_and_undoable():
    home = APP[APP.index("function _renderDriveHome(pane)") : APP.index("async function _renderHostRoot", APP.index("function _renderDriveHome(pane)"))]
    assert "does not delete or re-upload any Blossom bytes" in home
    assert "current version is retained" in home
    assert "await uiConfirm" in home
    assert "await FilesIdx.restore" in home
