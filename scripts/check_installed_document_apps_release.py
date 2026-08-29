#!/usr/bin/env python3
"""Run packaged Office/Email document behavior against the immutable installed ASAR.

The extraction/browser implementation remains in check_installed_document_apps.sh. This Python
entry point makes that installed-package check visible to checkall's check_*.py discovery.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "check_installed_document_apps.sh"


def main() -> int:
    asar = Path(os.environ.get("PC_INSTALLED_ASAR", "/opt/posterchan/resources/app.asar"))
    if not asar.is_file():
        print(f"SKIP installed ASAR is not available for the document-app release gate: {asar}")
        return 2
    return subprocess.run(["sh", str(GATE)], cwd=ROOT, env=os.environ.copy()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
