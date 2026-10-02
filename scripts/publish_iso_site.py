#!/usr/bin/env python3
"""Publish the iso.poster.place splash page: os/iso-site/index.html plus its two images.

The page lives in the same R2 bucket as the ISO and is reached at `/` through ONE Cloudflare URL
Rewrite rule on poster.place: `(http.host eq "iso.poster.place" and http.request.uri.path eq "/")`
-> static `/index.html`. Nothing else is rewritten, so `/posterchanos.iso` is served exactly as before.

This script writes ONLY index.html and site/*; it refuses any key that is the ISO or its checksum, so
a mistake here can never touch the download. The page reads the ISO's size, date and SHA-256 from the
live files at load time, so a new ISO release (scripts/publish_iso.sh) needs no change here.

Usage: scripts/publish_iso_site.py [--dry-run]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

FILES = [
    ("index.html", ROOT / "os/iso-site/index.html", "text/html; charset=utf-8", "public, max-age=300"),
    ("site/posterchan-os.webp", ROOT / "docs/promo/posterchan-os.webp", "image/webp", "public, max-age=86400"),
    ("site/posterchanos-desktop.webp", ROOT / "docs/promo/posterchanos-desktop.webp", "image/webp", "public, max-age=86400"),
]


def allowed(key: str) -> bool:
    return key == "index.html" or (key.startswith("site/") and ".." not in key)


def publish(r2, files=FILES, log=print) -> int:
    for key, path, ctype, cache in files:
        if not allowed(key):
            raise SystemExit(f"refusing to write {key!r}: only index.html and site/* belong to the splash page")
        body = path.read_bytes()
        r2.req("PUT", key, body=body, headers={"content-type": ctype, "cache-control": cache})
        r2.req("HEAD", key)
        log(f"published {key} ({len(body)} bytes)")
    return 0


def main(argv) -> int:
    if "--dry-run" in argv:
        for key, path, ctype, _ in FILES:
            print(f"would publish {key} <- {path.relative_to(ROOT)} ({ctype}, {path.stat().st_size} bytes)")
        return 0
    import publish_r2 as P
    return publish(P.R2(*P._creds()))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
