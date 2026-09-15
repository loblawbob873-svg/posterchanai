"""Desktop Folder Sync must use the primary writer even when its window has no opener."""
from pathlib import Path
import shutil
import subprocess

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("scenario", ["manual", "unknown_folder", "wrong_account", "automatic", "cancel",
                                      "stop", "concurrent", "changed_account", "verify", "cleanup", "background",
                                      "completed_then_automatic", "owner_unavailable", "heartbeat_lost"])
def test_detached_folder_sync_uses_only_the_primary_writer(scenario):
    result = subprocess.run(["node", str(HERE / "detached_sync_writer_runtime.cjs"), scenario],
                            text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr
