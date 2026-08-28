#!/usr/bin/env python3
"""Remove obsolete per-build Desktop releases without touching other products.

The Gentoo overlay pins an immutable ``desktop-vX.Y.Z`` asset.  Keep the newest
few for rollback, plus any version explicitly protected by the caller.  The
rolling ``desktop-latest`` release and every non-Desktop release are outside
this script's scope.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


TAG = re.compile(r"^desktop-v(\d+)\.(\d+)\.(\d+)$")
OVERLAY = Path("os/overlay/app-misc/posterchan-desktop")


def overlay_release(directory: Path = OVERLAY) -> str:
    """The immutable release Portage currently needs; refuse unsafe pruning if ambiguous."""
    ebuilds = sorted(directory.glob("posterchan-desktop-*.ebuild"))
    if len(ebuilds) != 1:
        raise RuntimeError(f"expected one overlay desktop ebuild in {directory}, found {len(ebuilds)}")
    version = ebuilds[0].name.removeprefix("posterchan-desktop-").removesuffix(".ebuild")
    tag = f"desktop-v{version}"
    if not TAG.fullmatch(tag):
        raise RuntimeError(f"overlay desktop ebuild has an invalid version: {ebuilds[0].name}")
    return tag


def stale_tags(releases: list[dict], keep: int, protect: set[str]) -> list[str]:
    versioned: list[tuple[tuple[int, int, int], str]] = []
    for release in releases:
        tag = str(release.get("tagName", ""))
        match = TAG.fullmatch(tag)
        if match:
            versioned.append((tuple(map(int, match.groups())), tag))
    versioned.sort(reverse=True)
    retained = {tag for _, tag in versioned[:keep]} | protect
    return [tag for _, tag in versioned if tag not in retained]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--keep", type=int, default=1)
    parser.add_argument("--protect", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.keep < 1:
        parser.error("--keep must be at least 1")

    raw = subprocess.check_output(
        ["gh", "release", "list", "--repo", args.repo, "--limit", "1000", "--json", "tagName"],
        text=True,
    )
    releases = json.loads(raw)
    # The overlay is consumed asynchronously by installed machines. CI may publish build N+1 while
    # the public overlay still pins N; deleting N in that window makes `emerge` 404. Protection is
    # automatic rather than another workflow argument somebody can forget.
    protect = set(args.protect)
    protect.add(overlay_release())
    doomed = stale_tags(releases, args.keep, protect)
    for tag in doomed:
        print(f"prune obsolete Desktop release {tag}")
        if not args.dry_run:
            # Delete the release and tag independently.  Some historical releases have already
            # lost their Git ref; ``gh release delete --cleanup-tag`` deletes the release and then
            # exits 1 when that absent ref returns 422, stopping the rest of an otherwise safe
            # retention pass halfway through.
            subprocess.run(
                ["gh", "release", "delete", tag, "--repo", args.repo, "--yes"],
                check=True,
            )
            subprocess.run(
                ["gh", "api", "--method", "DELETE", f"repos/{args.repo}/git/refs/tags/{tag}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    print(f"retained newest {args.keep} immutable Desktop releases; pruned {len(doomed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
