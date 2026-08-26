from pathlib import Path


SRC = (Path(__file__).parents[1] / "scripts" / "check_installed_code.py").read_text()


def test_installed_code_gate_uses_only_an_explicit_disposable_root():
    assert "root.startswith(\"/tmp/pc-code-installed.\")" in SRC
    assert "refusing a non-test root" in SRC


def test_installed_code_gate_exercises_ui_diff_restore_and_explorer_return():
    for marker in ('[data-code-view="git"]', '[data-git-diff="changed.js"]',
                   '[data-git-restore="changed.js"]', "Working tree clean",
                   '[data-code-view="explorer"]'):
        assert marker in SRC
    assert "pcHost.gitAction(root,'restore',['changed.js'])" in SRC
    assert '.uiconfirm-bg [data-uc="1"]' in SRC


def test_installed_code_gate_restores_user_editor_state_even_after_failure():
    assert "finally:" in SRC
    assert "__pcInstalledCodeBackup" in SRC
    assert "Object.assign(PCCode._state,b.state)" in SRC
