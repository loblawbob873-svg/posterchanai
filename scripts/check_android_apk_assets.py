#!/usr/bin/env python3
"""Fail when an Android APK does not contain this checkout's Concord client."""

import hashlib
import pathlib
import sys
import zipfile


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check_android_apk_assets.py APK SOURCE_ROOT", file=sys.stderr)
        return 2
    apk = pathlib.Path(sys.argv[1])
    root = pathlib.Path(sys.argv[2])
    source = (root / "static/js/client/concord.js").read_bytes()
    with zipfile.ZipFile(apk) as archive:
        packaged = archive.read("assets/public/static/js/client/concord.js")
        index = archive.read("assets/public/index.html").decode("utf-8", "replace")
    if packaged != source:
        print(f"stale APK Concord asset: source={digest(source)} apk={digest(packaged)}", file=sys.stderr)
        return 1
    if "static/js/client/app.js" not in index:
        print("APK index does not load the client entry point", file=sys.stderr)
        return 1
    print(f"APK Concord provenance OK: {digest(source)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
