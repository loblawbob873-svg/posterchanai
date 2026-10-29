"""update-posterchan says, before the restart, when the next start would not open this disk.

An update can bring a kernel and a new initramfs; the laptop's broken boot (2026-10-29: `root=UUID=` with no value)
was found only at the restart. The updater's own block, cut out of the shipped script and RUN under bash with a stub
pc-boot-check that passes or fails.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("script", ["os/bin/update-posterchan", "os/overlay/app-misc/posterchanos-shell/files/update-posterchan"])
@pytest.mark.parametrize("rc,warned", [(1, True), (0, False)])
def test_a_boot_that_would_not_start_is_said_before_the_restart(tmp_path, script, rc, warned):
    src = (ROOT / script).read_text()
    a = src.index("# AND WILL THE NEXT START OPEN THIS DISK?")
    block = src[a:src.index("\nfi\n", a) + 4]
    stub = tmp_path / "pc-boot-check"
    stub.write_text(f"#!/bin/sh\necho checked \"$@\"\nexit {rc}\n"); stub.chmod(0o755)
    r = subprocess.run([shutil.which("bash"), "-c", "RED=; OFF=\n" + block.replace("/usr/local/bin/pc-boot-check", str(stub))],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    assert "checked --quiet" in r.stdout
    assert ("do NOT restart" in r.stdout) == warned, r.stdout
