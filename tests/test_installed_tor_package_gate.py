from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_installed_tor_package.sh"


def test_gate_rejects_a_present_but_non_executable_tor(tmp_path):
    tree = tmp_path / "resources"
    binary = tree / "tor" / "tor" / "tor"
    data = tree / "tor" / "data"
    binary.parent.mkdir(parents=True)
    data.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho 'Tor version fixture'\n")
    binary.chmod(0o644)
    (data / "geoip").write_text("fixture")
    (data / "geoip6").write_text("fixture")
    result = subprocess.run([str(GATE)], env=dict(os.environ, PC_INSTALLED_RESOURCES=str(tree)),
                            text=True, capture_output=True)
    assert result.returncode != 0
    assert "not executable" in result.stderr


def test_gate_runs_the_binary_through_its_private_library_directory(tmp_path):
    tree = tmp_path / "resources"
    binary = tree / "tor" / "tor" / "tor"
    data = tree / "tor" / "data"
    binary.parent.mkdir(parents=True)
    data.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n"
                      "test \"$LD_LIBRARY_PATH\" = \"$(dirname \"$0\")\" || exit 9\n"
                      "echo 'Tor version fixture'\n")
    binary.chmod(0o755)
    (data / "geoip").write_text("fixture")
    (data / "geoip6").write_text("fixture")
    result = subprocess.run([str(GATE)], env=dict(os.environ, PC_INSTALLED_RESOURCES=str(tree)),
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "private libraries" in result.stdout
