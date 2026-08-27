from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()
OS = (ROOT / "static/js/client/os.js").read_text()


def test_packaged_shell_detects_replaced_asar_without_waiting_for_login():
    assert "path.join(process.resourcesPath,'app.asar')" in MAIN
    assert "s.dev}:${s.ino}:${s.size}:${s.mtimeMs}" in MAIN
    assert "setInterval(check,30000).unref()" in MAIN
    assert "watchInstalledBundle();" in MAIN


def test_every_surface_must_report_idle_before_canonical_restart():
    assert "pending:new Set(targets.map(w=>w.webContents.id))" in MAIN
    assert "_handoffReady.has(Number(w.webContents.id))" in MAIN
    assert "pending.pending.delete(e.sender.id)" in MAIN
    assert "if(pending.pending.size) return true" in MAIN
    assert "'/usr/local/bin/pc-shell-restart',[String(process.pid)]" in MAIN
    assert "if(!SHELL_MODE || diagnostic || _updateRestart)" in MAIN
    assert "updateIdle: (token)" in PRELOAD


def test_renderer_never_acknowledges_during_window_or_native_handoff_gesture():
    block = OS.split("if(p.startsWith('pc:update-installed:'))", 1)[1].split(
        "if(p === 'pc:start')", 1)[0]
    assert "w.gesturing||w.nativeHandoffToken" in block
    assert ".dragging,.resizing,.os-dragging" in block
    assert "if(!idle()){quiet=0;return;}" in block
    assert "if(++quiet<2)return" in block
    assert "pcWM.updateIdle(token)" in block


def test_update_ack_is_scoped_to_the_expected_renderer_and_token():
    assert "token!==pending.token" in MAIN
    assert "!pending.pending.has(e.sender.id)" in MAIN
