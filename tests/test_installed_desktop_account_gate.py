from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/check_installed_desktop_account.py").read_text(encoding="utf-8")


def test_installed_account_gate_uses_loopback_cdp_and_requires_authentication():
    assert "http://127.0.0.1:{PORT}" in SCRIPT
    assert "__PC.me && __PC.me()" in SCRIPT
    assert "no authenticated installed PosterChan page" in SCRIPT
    assert "SKIP installed Electron is not attached" in SCRIPT
    assert "sys.exit(2)" in SCRIPT


def test_installed_account_gate_checks_real_blossom_render_without_reading_names():
    assert "__PC.switchView('blossom')" in SCRIPT
    assert "folderTiles:q('.fx-home-tile')" in SCRIPT
    assert "const idx=__PC.filesIdx()" in SCRIPT
    assert "const pullOk=await idx.ensure()" in SCRIPT
    assert "'/client/files-index'" in SCRIPT
    assert 'files["clientFiles"] == files["serverFiles"]' in SCRIPT
    assert "syncedRoots:q('.syncroot')" in SCRIPT
    assert "textContent" not in SCRIPT
    # The diagnostics may count private index entries, but must never serialize the index itself.
    assert '"files": files' not in SCRIPT
    assert '"index":' not in SCRIPT


def test_installed_account_gate_uses_and_deletes_a_temporary_office_session():
    assert "posterchan-office-smoke.txt" in SCRIPT
    assert "'/wopi/files/'" in SCRIPT
    assert "body:'office smoke two\\n'" in SCRIPT
    assert '"/office-code/browser/"' in SCRIPT
    assert "{method:'DELETE'}" in SCRIPT
    assert "finally" in SCRIPT
