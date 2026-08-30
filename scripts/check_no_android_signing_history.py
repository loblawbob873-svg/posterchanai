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

# THE ONE RETIRED KEY, BY OBJECT ID, AND NOTHING ELSE.
#
# This blob is the Android signing key that was exposed and then ROTATED — see
# docs/ZAPSTORE_SIGNING_RECOVERY.md. It is not used for releases, is not loaded into CI (which signs
# from the ANDROID_KEYSTORE_BASE64 secret), and current APKs carry Android's signed
# proof-of-rotation from that certificate to the present one. The mainline was purged: it is
# reachable from neither HEAD, origin/master nor github/main.
#
# What keeps it reachable at all is a set of PUBLISHED RELEASE TAGS on the public mirror
# (apk-latest, desktop-v1.0.1068 and friends). Purging it from those would mean rewriting every
# release tag people hold download links to, to delete a key that is already dead — so the incident
# was closed by rotation instead, deliberately, and this guard should not keep reporting it as open.
#
# PINNED BY OBJECT ID, NOT BY PATH. A NEW key committed to the same path is a different blob and
# still fails, which is the whole point of this check; the test beside it proves exactly that.
RETIRED_SIGNING_OBJECTS = frozenset({"684f66c811c5829034f6c413b2cc937dc37583cc"})


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
        if object_id in RETIRED_SIGNING_OBJECTS:
            continue
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
    print("No live private Android signing containers are reachable in Git history "
          "(%d retired object(s) accounted for — see docs/ZAPSTORE_SIGNING_RECOVERY.md)."
          % len(RETIRED_SIGNING_OBJECTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
