"""The publish script's own read-back must actually run on the far side.

It sent awk a field reference through two levels of quoting as `\\$1`. Bash turned that into a
literal backslash, the remote awk died with "unexpected character '\\'", and under `set -e` the
failed command substitution aborted the script AFTER the renames — so a correct publish printed
neither its success nor its failure line and exited nonzero. That reads as "the ISO did not go
out" about an ISO that had, which is the reading most likely to cause a needless republish.

This RUNS the read-back the way the script builds it, against a real sidecar file, with `ssh`
replaced by a stub that executes what it was handed.
"""
from pathlib import Path
import re
import subprocess

SRC = (Path(__file__).resolve().parents[1] / "scripts/publish_iso.sh").read_text(encoding="utf-8")


def _readback_line():
    line = next(l for l in SRC.splitlines() if l.startswith("PUBLISHED_SHA="))
    return line


def test_the_sidecar_read_back_actually_runs(tmp_path):
    sidecar = tmp_path / "posterchanos.iso.sha256"
    want = "51c636e1e7f38ffb1dc10e609d94e735631936e0a9ce46fe99ee6993bd493c2b"
    sidecar.write_text(f"{want}  posterchanos.iso\n")
    stub = tmp_path / "ssh"
    # The stub ignores the host and runs the remote command locally, which is what the far side does.
    stub.write_text('#!/bin/bash\nshift\neval "$@"\n')
    stub.chmod(0o755)
    script = (f'set -euo pipefail\nPATH="{tmp_path}:$PATH"\n'
              f'PUBLISH_HOST=stub\nCHECKSUM_PATH="{sidecar}"\n'
              + _readback_line() + '\nprintf "%s" "$PUBLISHED_SHA"\n')
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == want, f"read back {out.stdout!r}, stderr {out.stderr!r}"


def test_it_does_not_reach_for_awk_through_two_levels_of_quoting():
    assert "awk" not in _readback_line(), "the escaping that broke this is easy to reintroduce"


def test_an_unreadable_sidecar_says_the_iso_was_published():
    """Exiting nonzero is right; implying the upload failed is not — the renames already happened."""
    assert "Published, but the sidecar could not be read back" in SRC
    empty_guard = SRC.index('if [[ -z "$PUBLISHED_SHA" ]]')
    mismatch = SRC.index('!= "$LOCAL_SHA"', empty_guard)
    assert empty_guard < mismatch, "an empty read must be distinguished from a mismatch"
