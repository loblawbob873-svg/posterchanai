from pathlib import Path
import os
import subprocess
import sys

from scripts import checkall


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


def test_document_app_gate_is_discovered_and_serialized_by_release_runner():
    jobs = {job["name"]: job for job in checkall.discover()}
    job = jobs["check_installed_document_apps_release"]
    assert job["registered"] is True
    assert job["serial"] is True
    assert job["secs"] == 420


def test_discoverable_document_gate_skips_without_an_installed_artifact(tmp_path):
    gate = ROOT / "scripts" / "check_installed_document_apps_release.py"
    env = {**os.environ, "PC_INSTALLED_ASAR": str(tmp_path / "missing.asar")}
    result = subprocess.run([sys.executable, str(gate)], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "SKIP installed ASAR is not available for the document-app release gate" in result.stdout
