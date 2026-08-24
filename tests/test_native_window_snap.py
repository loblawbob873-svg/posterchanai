from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_native_snap_helper_is_shipped_and_bound_in_both_os_configs():
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    assert helper.exists()
    code = helper.read_text()
    assert '"left", "right", "max", "edge"' in code
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


def test_native_titlebars_use_the_posterchan_palette():
    for cfg in (
        ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config",
        ROOT / "os/gentoo.sh",
    ):
        src = cfg.read_text()
        assert "client.focused          #241438" in src
        assert "client.unfocused        #100d18" in src


def test_firefox_and_telegram_cannot_lose_the_native_snap_container():
    for cfg in (
        ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config",
        ROOT / "os/gentoo.sh",
    ):
        src = cfg.read_text()
        assert '[app_id="firefox"] floating enable, border normal 3' in src
        assert '[class="(?i)^firefox$"] floating enable, border normal 3' in src
        assert '[app_id="org.telegram.desktop"] floating enable, border normal 3' in src
        assert '[class="(?i)^(TelegramDesktop|telegram-desktop)$"] floating enable, border normal 3' in src


def test_existing_private_configs_gain_explicit_native_window_rules():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert 'native_rule in' in ebuild
    assert '[app_id="firefox"] floating enable, border normal 3' in ebuild
    assert '[app_id="org.telegram.desktop"] floating enable, border normal 3' in ebuild


def test_dragging_a_titlebar_to_an_output_edge_snaps_without_stealing_app_clicks():
    helper = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap").read_text()
    assert '"edge"' in helper
    assert 'side = "left"' in helper and 'side = "right"' in helper
    for cfg in (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config",
                ROOT / "os/gentoo.sh"):
        src = cfg.read_text()
        assert "bindsym --release button1 exec /usr/local/bin/pc-window-snap edge" in src
        assert "--whole-window" not in src[src.index("pc-window-snap edge") - 80:src.index("pc-window-snap edge")]
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert "pc-window-snap pc-key" in ebuild
