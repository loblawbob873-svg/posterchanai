from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gate_runs_installed_office_workspace_and_mail_browser_runtime():
    gate = (ROOT / "scripts/check_installed_document_apps.sh").read_text(encoding="utf-8")
    assert "www/static/js/client/app.js" in gate
    assert "www/static/js/client/os.js" in gate
    assert "PC_INSTALLED_APP_JS=" in gate
    assert "PC_INSTALLED_OS_JS=" in gate
    assert "installed_document_workspace_sim.js" in gate
    assert "check_mail_mobile.py" in gate


def test_mail_browser_harness_accepts_the_installed_app_payload():
    src = (ROOT / "scripts/check_mail_mobile.py").read_text(encoding="utf-8")
    assert 'os.environ.get("PC_INSTALLED_APP_JS")' in src
