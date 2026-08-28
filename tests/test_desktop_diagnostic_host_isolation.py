from pathlib import Path


SOURCE = (Path(__file__).parents[1] / "desktop" / "main.js").read_text()


def test_installed_diagnostic_cannot_provision_or_switch_the_host_session():
    """A nested installed-app check must never replace tty1 or the operator's desktop."""
    provision = SOURCE.index("ipcMain.handle('pc:os:provision'")
    switch = SOURCE.index("ipcMain.handle('pc:os:switch'")
    logout = SOURCE.index("ipcMain.handle('pc:os:logout'")
    assert "if (diagnostic) return" in SOURCE[provision:switch]
    assert "if (diagnostic) return" in SOURCE[switch:logout]
    assert "if (diagnostic) return" in SOURCE[logout:SOURCE.index("ipcMain.on('pc:os:bootstrap'")]


def test_diagnostic_denial_happens_before_privileged_helpers_are_spawned():
    provision = SOURCE[SOURCE.index("ipcMain.handle('pc:os:provision'"):
                       SOURCE.index("ipcMain.handle('pc:os:provisioned'")]
    switch = SOURCE[SOURCE.index("ipcMain.handle('pc:os:switch'"):
                    SOURCE.index("ipcMain.on('pc:os:bootstrap'")]
    assert provision.index("if (diagnostic)") < provision.index("pc-provision-user")
    assert switch.index("if (diagnostic)") < switch.index("pc-session-switch")
