"""Is the INSTALLED desktop bundle the commit this working tree is on?

ONE copy of this question, because the wrong answer is a FALSE RED that blames an innocent feature.
`check_installed_code_package_release` reported five problems against PosterChan Code — including
its discard dialog "not saying what it would lose" while quoting, verbatim, the sentence that was
REPLACED in this very working tree. The gate was reading /opt/posterchan/resources/app.asar, built
several commits earlier, and describing it as a defect in code that had already been fixed. That is
recorded in memory as `project_installed_asar_stale_false_red.md`; it happened again because the
guard lived in one gate and not in the others.

An UNREADABLE stamp is deliberately not stale: with nothing to compare there is nothing to judge,
and refusing on that basis would make every bundle built before stamping a permanent skip.
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ASAR_BIN = ROOT / "desktop" / "node_modules" / ".bin" / "asar"


def installed_stamp(asar: Path) -> str:
    """The commit the installed bundle was built from, or '' when it cannot be read."""
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


def head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def stale_reason(asar: Path, what: str) -> str:
    """A sentence to print before exiting 2, or '' when this bundle may be judged.

    `PC_ALLOW_STALE_INSTALL` opts out — for deliberately gating an older bundle.
    """
    if os.environ.get("PC_ALLOW_STALE_INSTALL"):
        return ""
    stamp, at = installed_stamp(asar), head()
    # Compared as prefixes: the bundle stamps an abbreviated sha and `git rev-parse --short` can
    # abbreviate to a different length on a bigger repo.
    if stamp and at and not (stamp.startswith(at) or at.startswith(stamp)):
        return (f"installed build is {stamp}, the repo is at {at} — this gate tests the INSTALLED "
                f"bundle at {asar} and cannot speak for your working tree ({what}). Deploy, or "
                f"point PC_INSTALLED_ASAR at the bundle you mean to gate.")
    return ""
