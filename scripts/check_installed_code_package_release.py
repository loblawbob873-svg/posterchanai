#!/usr/bin/env python3
"""Run packaged PosterChan Code/Git behavior from the immutable installed ASAR.

The shell gate performs ASAR extraction, a disposable real-Git simulation, and the browser editor
check. This Python entry point makes that check visible to checkall's check_*.py discovery.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _installed_stamp import stale_reason  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "check_installed_code_package.sh"


def main() -> int:
    asar = Path(os.environ.get("PC_INSTALLED_ASAR", "/opt/posterchan/resources/app.asar"))
    if not asar.is_file():
        print(f"SKIP installed ASAR is not available for the Code package release gate: {asar}")
        return 2
    # A STALE BUNDLE IS NOT A DEFECT IN THIS TREE. Without this the gate reported five problems
    # against PosterChan Code — including its discard dialog "not saying what it would lose" while
    # quoting the exact sentence this working tree had already replaced. Shared with the other
    # installed-* gates so the guard cannot exist in one and not the others.
    reason = stale_reason(asar, "packaged Code/Git behaviour")
    if reason:
        print("SKIP " + reason)
        return 2
    return subprocess.run(["sh", str(GATE)], cwd=ROOT, env=os.environ.copy()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
