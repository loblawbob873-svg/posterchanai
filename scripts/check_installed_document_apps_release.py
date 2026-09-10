#!/usr/bin/env python3
"""Run packaged Office/Email document behavior against the immutable installed ASAR.

The extraction/browser implementation remains in check_installed_document_apps.sh. This Python
entry point makes that installed-package check visible to checkall's check_*.py discovery.

IT TESTS THE INSTALLED BUILD, AND A STALE ONE MUST NOT BE REPORTED AS A BROKEN FEATURE.

Measured on server1: /opt/posterchan carried build 0d2b19b from four days earlier while the repo was
at eabe9f7c9, and this gate reported six problems — "the message itself gets 0.26 of the screen",
"select-all is not reachable" — every one of them describing an old bundle nobody had touched.
Pointed at the current build the same gate answers `OK email mobile checks passed`.

That is not a harmless red. It reads exactly like a live mobile-mail regression, and it is the
second time this shape has cost real work here: a `check_installed_*` script reads /opt/posterchan,
never the working tree, so it blames whichever feature its assertions happen to name. So the build
stamp is compared FIRST, and a mismatch is a SKIP carrying both commits — "could not run", which the
suite prints as a skip with its reason and never as a pass.

Set PC_INSTALLED_ASAR to gate a specific bundle (that is how the mismatch above was diagnosed), or
PC_ALLOW_STALE_INSTALL=1 to run it against whatever is installed anyway.
"""
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "check_installed_document_apps.sh"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _installed_stamp import stale_reason  # noqa: E402


def main() -> int:
    asar = Path(os.environ.get("PC_INSTALLED_ASAR", "/opt/posterchan/resources/app.asar"))
    if not asar.is_file():
        print(f"SKIP installed ASAR is not available for the document-app release gate: {asar}")
        return 2

    # ONE copy of "is this bundle the commit we are on" — see scripts/_installed_stamp.py.
    reason = stale_reason(asar, "the document apps")
    if reason:
        print("SKIP " + reason)
        return 2

    return subprocess.run(["sh", str(GATE)], cwd=ROOT, env=os.environ.copy()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
