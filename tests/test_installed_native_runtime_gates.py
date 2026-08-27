from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installed_native_gates_recognize_the_packaged_desktop_app_id():
    for name in ("check_installed_native_focus.py", "check_installed_native_handoff.py",
                 "check_installed_native_snap.py"):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "place\\.poster\\.desktop" in src, name
        assert "posterchan(-desktop)?" in src, name


def test_focus_gate_does_not_require_account_data_to_test_the_os_shell():
    src = (ROOT / "scripts" / "check_installed_native_focus.py").read_text(encoding="utf-8")
    assert "choose_authenticated_page" not in src
    assert "async def choose_native_page" in src
    assert "if(!PCOS.isOn())PCOS.enter()" in src
    assert "document.querySelectorAll('.osw-native').length>0" in src
