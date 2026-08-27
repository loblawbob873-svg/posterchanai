from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installed_native_gates_recognize_the_packaged_desktop_app_id():
    for name in ("check_installed_native_focus.py", "check_installed_native_snap.py"):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "place\\.poster\\.desktop" in src, name
        assert "posterchan(-desktop)?" in src, name


def test_focus_gate_does_not_require_account_data_to_test_the_os_shell():
    src = (ROOT / "scripts" / "check_installed_native_focus.py").read_text(encoding="utf-8")
    assert "choose_authenticated_page" not in src
    assert "async def choose_native_page" in src
    assert "if(!PCOS.isOn())PCOS.enter()" in src
    assert "document.querySelectorAll('.osw-native').length>0" in src


def test_live_native_gates_can_target_one_exact_window_and_refuse_ambiguity():
    for name in ("check_installed_native_focus.py", "check_installed_native_snap.py",
                 "check_installed_native_handoff.py"):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'os.environ.get("PC_NATIVE_APP_ID")' in src, name
        assert "data-pc-check-native" in src, name
        assert "rows.length===1" in src, name


def test_handoff_gate_requires_exact_renderer_and_native_window_identity():
    src = (ROOT / "scripts" / "check_installed_native_handoff.py").read_text(encoding="utf-8")
    assert "snap.shellId" in src
    assert "requires PC_NATIVE_APP_ID" in src
    assert "data-pc-check-native" in src


def test_native_frames_publish_their_compositor_identity():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    assert "w.el.dataset.native=String(id)" in src


def test_snapshot_publishes_the_calling_renderers_exact_shell_identity():
    src = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    block = src.split("ipcMain.handle('pc:wm:snapshot'", 1)[1].split("});", 1)[0]
    assert "_shellScopes.get(e.sender.id)" in block
    assert "_shellSurfaces.get(scope.output)" in block
    assert "shellId:" in block
