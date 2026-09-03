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
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "check_installed_document_apps.sh"
ASAR_BIN = ROOT / "desktop" / "node_modules" / ".bin" / "asar"


def _installed_stamp(asar: Path) -> str:
    """The commit the installed bundle was built from, or '' if it cannot be read.

    Unreadable is deliberately NOT stale: without a stamp there is nothing to compare, and refusing
    to run on that basis would turn every bundle built before stamping into a permanent skip."""
    if not ASAR_BIN.is_file():
        return ""
    try:
        with tempfile.TemporaryDirectory() as td:
            done = subprocess.run([str(ASAR_BIN), "extract-file", str(asar), "www/index.html"],
                                  cwd=td, capture_output=True, timeout=120)
            if done.returncode != 0:
                return ""
            html = (Path(td) / "index.html").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    found = re.search(r'window\.__PC_BUILD="([^"]*)"', html)
    return found.group(1) if found else ""


def _head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def main() -> int:
    asar = Path(os.environ.get("PC_INSTALLED_ASAR", "/opt/posterchan/resources/app.asar"))
    if not asar.is_file():
        print(f"SKIP installed ASAR is not available for the document-app release gate: {asar}")
        return 2

    if not os.environ.get("PC_ALLOW_STALE_INSTALL"):
        stamp, head = _installed_stamp(asar), _head()
        # Compared as prefixes: the bundle stamps an abbreviated sha and `git rev-parse --short` can
        # abbreviate to a different length on a bigger repo.
        if stamp and head and not (stamp.startswith(head) or head.startswith(stamp)):
            print(f"SKIP installed build is {stamp}, the repo is at {head} — this gate tests the "
                  f"INSTALLED bundle at {asar} and cannot speak for your working tree. Deploy, or "
                  f"point PC_INSTALLED_ASAR at the bundle you mean to gate.")
            return 2

    return subprocess.run(["sh", str(GATE)], cwd=ROOT, env=os.environ.copy()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
