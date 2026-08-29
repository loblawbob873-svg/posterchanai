from pathlib import Path


SRC = (Path(__file__).parents[1] / "scripts" / "check_installed_code.py").read_text()
EMBEDDED = SRC.replace(r'\"', '"')


def test_installed_code_gate_uses_only_an_explicit_disposable_root():
    assert "root.startswith(\"/tmp/pc-code-installed.\")" in SRC
    assert "refusing a non-test root" in SRC
    assert "choose_authenticated_page" not in SRC
    assert 'startswith("app://posterchan/")' in SRC


def test_installed_code_gate_exercises_ui_diff_restore_and_explorer_return():
    for marker in ('#pcc-open-folder', '[data-code-view="git"]', '[data-git-diff="changed.js"]',
                   '[data-git-restore="changed.js"]', "Working tree clean",
                   '[data-code-view="explorer"]'):
        assert marker in EMBEDDED
    assert 'contextBridge may appear writable' in SRC
    assert "openFolder.click()" not in SRC
    assert "const listed=await pcHost.list(root)" in SRC
    assert "pcHost.gitAction(root,'restore',['changed.js'])" in SRC
    assert '.uiconfirm-bg [data-uc="1"]' in EMBEDDED


def test_installed_code_gate_restores_user_editor_state_even_after_failure():
    assert "finally:" in SRC
    assert "__pcInstalledCodeBackup" in SRC
    assert "Object.assign(PCCode._state,b.state)" in SRC
    assert "pcHost.pickDirectory=b.pickDirectory" in SRC


def test_installed_code_gate_serializes_results_across_workspace_repaints():
    assert "document.querySelector('[data-view=\"code\"]')" in EMBEDDED
    assert "PCOS.routeView&&PCOS.routeView('code')" not in SRC
    assert 'await asyncio.sleep(1)' in SRC
    assert 'await cdp.eval(PREPARE' in SRC
    assert 'async def wait_value' in SRC
    assert 'file_selector = json.dumps' in SRC
    assert "await cdp.eval(\"document.querySelector('[data-code-view=" in EMBEDDED
