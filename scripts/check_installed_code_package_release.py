#!/usr/bin/env python3
"""Run packaged PosterChan Code/Git behavior from the immutable installed ASAR.

The shell gate performs ASAR extraction, a disposable real-Git simulation, and the browser editor
check. This Python entry point makes that check visible to checkall's check_*.py discovery.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "check_installed_code_package.sh"


def main() -> int:
    asar = Path(os.environ.get("PC_INSTALLED_ASAR", "/opt/posterchan/resources/app.asar"))
    if not asar.is_file():
        print(f"SKIP installed ASAR is not available for the Code package release gate: {asar}")
        return 2
    return subprocess.run(["sh", str(GATE)], cwd=ROOT, env=os.environ.copy()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
