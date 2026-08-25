#!/usr/bin/env python3
"""Verify the public Gentoo overlay and immutable desktop release agree with this checkout."""
import os
import re
import subprocess
import tempfile
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_REL = os.path.join("app-misc", "posterchan-desktop")
LOCAL = os.path.join(ROOT, "os", "overlay", PKG_REL)
OVERLAY = "https://gentoo.poster.place/posterchan-overlay.git"
RELEASE = "https://github.com/loblawbob873-svg/posterchanai/releases/download"


def one_ebuild(path):
    names = sorted(x for x in os.listdir(path) if x.endswith(".ebuild"))
    if len(names) != 1:
        raise RuntimeError(f"expected one desktop ebuild in {path}, found {names}")
    return names[0]


def main():
    with tempfile.TemporaryDirectory(prefix="pc-overlay-live-") as td:
        # gentoo.poster.place intentionally serves a dumb-HTTP repository; shallow negotiation is
        # unavailable there, so a --depth clone fails before reading a single overlay file.
        run = subprocess.run(["git", "clone", "-q", OVERLAY, td],
                             capture_output=True, text=True, timeout=120)
        if run.returncode:
            raise RuntimeError("public overlay clone failed: " + run.stderr.strip()[:240])
        public = os.path.join(td, PKG_REL)
        local_ebuild, public_ebuild = one_ebuild(LOCAL), one_ebuild(public)
        if local_ebuild != public_ebuild:
            raise RuntimeError(f"public overlay has {public_ebuild}, checkout has {local_ebuild}")
        for name in (local_ebuild, "Manifest"):
            with open(os.path.join(LOCAL, name), "rb") as fh:
                ours = fh.read()
            with open(os.path.join(public, name), "rb") as fh:
                theirs = fh.read()
            if ours != theirs:
                raise RuntimeError(f"public overlay {name} differs from the checkout")

        version = local_ebuild.removeprefix("posterchan-desktop-").removesuffix(".ebuild")
        manifest = open(os.path.join(public, "Manifest"), encoding="utf-8").read().strip()
        m = re.fullmatch(r"DIST \S+ (\d+) BLAKE2B [0-9a-f]{128} SHA512 ([0-9a-f]{128})", manifest)
        if not m:
            raise RuntimeError("public desktop Manifest is malformed")
        size, digest = int(m.group(1)), m.group(2)
        base = f"{RELEASE}/desktop-v{version}/PosterChan-{version}-linux-x64.tar.zst"
        with urllib.request.urlopen(base + ".sha512", timeout=120) as r:
            published = r.read().decode().split()[0]
        if published != digest:
            raise RuntimeError("public release checksum differs from the Gentoo Manifest")
        req = urllib.request.Request(base, method="HEAD")
        with urllib.request.urlopen(req, timeout=120) as r:
            length = int(r.headers.get("Content-Length") or 0)
        if length and length != size:
            raise RuntimeError(f"release is {length} bytes but Manifest promises {size}")
        print(f"OK public overlay and immutable desktop {version} agree ({size} bytes)")


if __name__ == "__main__":
    main()
