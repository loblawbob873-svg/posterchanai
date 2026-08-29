#!/usr/bin/env python3
"""Fail when private Android signing containers remain reachable in Git history.

This check deliberately examines object names, not object contents.  Its diagnostics
therefore identify the object that must be purged without copying key bytes into CI
logs (or into another working-tree file).
"""

from pathlib import Path
import argparse
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SIGNING_SUFFIXES = frozenset({".jks", ".keystore", ".p12", ".pfx"})


class HistoryUnavailable(RuntimeError):
    """The repository cannot prove that its complete reachable history is clean."""


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def reachable_private_signing_paths(repo):
    """Return sorted ``(object_id, path)`` findings from every reachable ref."""
    repo = Path(repo)
    try:
        shallow = _git(repo, "rev-parse", "--is-shallow-repository").strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HistoryUnavailable("not a readable Git repository") from exc
    if shallow != "false":
        raise HistoryUnavailable("repository history is shallow or incomplete")

    findings = set()
    try:
        objects = _git(repo, "rev-list", "--objects", "--all")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HistoryUnavailable("reachable Git objects could not be enumerated") from exc
    for line in objects.splitlines():
        fields = line.split(" ", 1)
        if len(fields) != 2:
            continue
        object_id, historical_path = fields
        if Path(historical_path).suffix.lower() in PRIVATE_SIGNING_SUFFIXES:
            findings.add((object_id, historical_path))
    return sorted(findings, key=lambda item: (item[1].casefold(), item[0]))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        findings = reachable_private_signing_paths(args.repo)
    except HistoryUnavailable as exc:
        print(f"Android signing-history guard failed: {exc}", file=sys.stderr)
        return 1

    if findings:
        print("Android signing-history guard failed:", file=sys.stderr)
        for object_id, historical_path in findings:
            print(
                f"  - reachable private signing blob: {object_id} {historical_path}",
                file=sys.stderr,
            )
        return 1
    print("No private Android signing containers are reachable in Git history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
