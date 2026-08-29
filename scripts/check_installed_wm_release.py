#!/usr/bin/env python3
"""Run the immutable-ASAR WM/clipboard/Alt+Tab release gate.

The implementation lives in the shell gate because it extracts several files from app.asar before
running Node simulators.  This Python entry point is intentional: checkall discovers check_*.py,
so leaving the installed gate only as check_*.sh silently omitted it from the release suite.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "check_installed_wm_package.sh"


def main() -> int:
    asar = Path(os.environ.get("PC_INSTALLED_ASAR", "/opt/posterchan/resources/app.asar"))
    if not asar.is_file():
        print(f"SKIP installed ASAR is not available for the WM release gate: {asar}")
        return 2
    result = subprocess.run(["sh", str(GATE)], cwd=ROOT, env=os.environ.copy())
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
