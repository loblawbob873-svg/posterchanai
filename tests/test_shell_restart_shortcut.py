from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ctrl_alt_backspace_restarts_only_the_posterchan_shell():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    line = next(x for x in cfg.splitlines() if "Ctrl+Mod1+BackSpace" in x)
    assert "--release --no-repeat" in line
    assert "pkill -f posterchan[-]desktop" in line
    assert "/usr/local/bin/pc-shell-start" in line
    assert "swaymsg exit" not in line
    assert "systemctl" not in line


def test_installer_ships_the_same_recovery_binding():
    installer = (ROOT / "os/gentoo.sh").read_text()
    assert "Ctrl+Mod1+BackSpace" in installer
    assert "pkill -f posterchan[-]desktop" in installer
