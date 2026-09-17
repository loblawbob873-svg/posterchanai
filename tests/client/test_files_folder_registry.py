"""Folder imports register exactly the folders their files are filed under (long paths and encrypted
subfolders included), and restored/encrypted files keep their type icons. Runs the SHIPPED code under
node — see tests/client/files_folder_registry_sim.js."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_folder_registry_and_restored_icons():
    r = subprocess.run(["node", str(ROOT / "tests/client/files_folder_registry_sim.js")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "ALL OK" in r.stdout, r.stdout + r.stderr
