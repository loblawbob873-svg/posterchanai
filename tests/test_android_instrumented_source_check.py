"""scripts/android_instrumented.sh decides "the device ran nothing" when androidTest has no sources -- and it must not
decide that about a tree that HAS them.

It used `find ... | grep -q .` under `set -o pipefail`: grep exits at the first name, find is killed by SIGPIPE writing
the rest, the pipeline fails, and the `!` read that as "no sources". CI run 40373764324 (2026-10-26) skipped every
instrumented test that way with all 48 checked out. The 48 files are 4.3 KB of names, just over one write, so it is a
race there; 3000 files make it certain. This runs the SHIPPED check against such a tree.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/android_instrumented.sh").read_text()


def _check():
    m = re.search(r"^if (.*androidTest.*); then\n\s*skip \"mobile/android/app/src/androidTest has no sources", SCRIPT, re.M)
    assert m, "the no-sources check moved"
    return m.group(1)


def _says_no_sources(tree: Path, n: int) -> bool:
    src = tree / "mobile/android/app/src/androidTest/java"
    src.mkdir(parents=True)
    for i in range(n):
        (src / f"Probe{i}DeviceTest.java").write_text("")
    script = f"set -uo pipefail\nif {_check()}; then echo NONE; else echo SOME; fi\n"
    out = subprocess.run(["bash", "-c", script], cwd=tree, capture_output=True, text=True, timeout=60).stdout
    return "NONE" in out


def test_a_big_test_tree_is_not_called_empty(tmp_path):
    assert not any(_says_no_sources(tmp_path / str(i), 3000) for i in range(5)), \
        "the check called 3000 test files 'no sources' -- the device would run nothing and the job would say so"


def test_an_empty_tree_is_still_called_empty(tmp_path):
    assert _says_no_sources(tmp_path, 0)
