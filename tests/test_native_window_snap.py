from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_native_snap_helper_is_shipped_and_bound_in_both_os_configs():
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    assert helper.exists()
    code = helper.read_text()
    assert '"left", "right", "max"' in code
    assert '"move", "absolute", "position"' in code
    for cfg in (
        ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config",
        ROOT / "os/gentoo.sh",
    ):
        src = cfg.read_text()
        assert "$mod+Left  exec /usr/local/bin/pc-window-snap left" in src
        assert "$mod+Right exec /usr/local/bin/pc-window-snap right" in src
        assert "$mod+Up    exec /usr/local/bin/pc-window-snap max" in src


def test_existing_identity_configs_are_migrated_to_native_snap():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert "pc-window-snap pc-key" in ebuild
    assert "focus output/d" in ebuild
