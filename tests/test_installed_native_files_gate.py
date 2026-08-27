from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/check_installed_native_files.py").read_text(encoding="utf-8")


def test_native_files_gate_is_installed_runtime_and_account_independent():
    assert "app://posterchan/" in SCRIPT
    assert "choose_authenticated_page" not in SCRIPT
    assert "native_files_check(fixture)" in SCRIPT
    assert 'result["path"] and result["rows"] == 2' in SCRIPT
    assert '{"code", "host"}.issubset(result["confChoices"])' in SCRIPT
    assert 'result["preview"]' in SCRIPT
    assert "tempfile.TemporaryDirectory(" in SCRIPT
    assert 'prefix="posterchan-installed-files-"' in SCRIPT
    assert 'os.environ.get("PC_NATIVE_FILES_FIXTURE"' in SCRIPT
    assert "if not supplied:" in SCRIPT
