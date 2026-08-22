from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_quick_settings_replaces_screenshot_with_keep_awake():
    src = (ROOT / "static/js/client/osshell.js").read_text(encoding="utf-8")
    start = src.index("function quickHTML")
    quick = src[start:src.index("const rows = []", start)]
    assert "<b>Keep Awake</b>" in quick
    assert "ICO('eye')" in quick
    assert "<b>Screenshot</b>" not in quick
    assert "setKeepAwake" in src


def test_idle_helper_has_a_session_hold_in_both_shipped_copies():
    for rel in ("os/bin/pc-idle", "os/overlay/app-misc/posterchanos-shell/files/pc-idle"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert 'hold)' in src
        assert 'posterchan-keep-awake' in src
        assert '[ ! -e "$HOLD" ] || exit 0' in src
