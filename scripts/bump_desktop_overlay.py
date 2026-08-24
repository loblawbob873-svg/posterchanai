#!/usr/bin/env python3
"""Point the Gentoo overlay at the newest published desktop build.

    venv-unified/bin/python scripts/bump_desktop_overlay.py            # newest
    venv-unified/bin/python scripts/bump_desktop_overlay.py 1.0.831    # a specific one
    venv-unified/bin/python scripts/bump_desktop_overlay.py --check    # report, change nothing

WHY THIS IS A SCRIPT. The desktop's version is `1.0.<github run number>`, so it bumps on EVERY push
— the overlay is behind again within minutes of anybody touching the client. Keeping it current by
hand means: find the newest tag, download 130 MB, compute BLAKE2B and SHA512, rename the ebuild,
rewrite the Manifest. That got done wrong three times (1.0.818, 1.0.825, 1.0.828), and each time
`emerge posterchan-desktop` simply could not run.

WHAT IT WILL NOT DO IS GUESS. The digests come from the bytes, and the SHA512 is cross-checked
against the .sha512 GitHub publishes beside the tarball — if those disagree the download is wrong
and nothing is written. A Manifest is a promise about exact bytes; a wrong one fails at emerge time
on somebody else's machine.

Being a version or two behind is FINE now and no longer breaks anything: the ebuild fetches
`desktop-v<version>`, a tag CI writes once and never touches. This is for when you want PosterChanOS
machines to get a newer desktop, not something to run on every commit.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchan-desktop")
REPO = "loblawbob873-svg/posterchanai"
BASE = f"https://github.com/{REPO}/releases/download"


def _current():
    names = [f for f in os.listdir(PKG) if f.endswith(".ebuild")]
    if len(names) != 1:
        sys.exit(f"FAIL  expected one ebuild, found {names}")
    return names[0][len("posterchan-desktop-"):-len(".ebuild")], names[0]


def _newest_tag():
    """The highest desktop-v<version> RELEASE — not `desktop-latest`, whose assets are deleted and
    re-created on every build."""
    out = subprocess.run(["gh", "release", "list", "--repo", REPO, "--limit", "60",
                          "--json", "tagName"], capture_output=True, text=True, timeout=120)
    if out.returncode:
        sys.exit(f"FAIL  could not list releases: {out.stderr.strip()[:200]}")
    vers = []
    for r in json.loads(out.stdout or "[]"):
        m = re.fullmatch(r"desktop-v(\d+\.\d+\.\d+)", r.get("tagName", ""))
        if m:
            vers.append(m.group(1))
    if not vers:
        sys.exit("FAIL  no desktop-v* releases — has the workflow published one yet?")
    return max(vers, key=lambda v: [int(x) for x in v.split(".")])


def _fetch(url):
    with urllib.request.urlopen(url, timeout=300) as r:
        return r.read()


def main():
    args = [a for a in sys.argv[1:]]
    check_only = "--check" in args
    args = [a for a in args if not a.startswith("-")]
    cur, cur_file = _current()
    want = args[0] if args else _newest_tag()

    print(f"overlay pins : {cur}")
    print(f"newest build : {want}")
    if cur == want:
        print("OK  already current")
        return 0
    if check_only:
        print(f"BEHIND  run without --check to move it to {want}")
        return 1

    name = f"PosterChan-{want}-linux-x64.tar.zst"
    url = f"{BASE}/desktop-v{want}/{name}"
    print(f"downloading  {url}")
    try:
        blob = _fetch(url)
    except Exception as e:
        sys.exit(f"FAIL  {want} has no per-version release ({e}) — CI publishes desktop-v<version>; "
                 "a build from before that change has only the rolling tag, which is deleted.")

    sha512 = hashlib.sha512(blob).hexdigest()
    blake2b = hashlib.blake2b(blob).hexdigest()
    try:
        published = _fetch(url + ".sha512").decode().split()[0].strip()
    except Exception:
        published = ""
    if published and published != sha512:
        sys.exit("FAIL  the download does not match the .sha512 GitHub published beside it — "
                 "writing this Manifest would fail at emerge time on somebody else's machine")
    print(f"size {len(blob)}  sha512 verified against the published checksum"
          if published else f"size {len(blob)}  (no published .sha512 to cross-check)")

    new_file = f"posterchan-desktop-{want}.ebuild"
    subprocess.run(["git", "mv", os.path.join(PKG, cur_file), os.path.join(PKG, new_file)],
                   cwd=ROOT, check=True)
    with open(os.path.join(PKG, "Manifest"), "w", encoding="utf-8") as fh:
        fh.write(f"DIST posterchan-desktop-{want}.tar.zst {len(blob)} "
                 f"BLAKE2B {blake2b} SHA512 {sha512}\n")
    print(f"OK  overlay now pins {want}")
    print("    run: venv-unified/bin/python -m pytest tests/test_gentoo_overlay_pins_resolve.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
