#!/usr/bin/env python3
"""Print the build number baked into a PosterChan APK, or fail closed."""
import re
import sys
import zipfile


def embedded_build(path: str) -> int:
    with zipfile.ZipFile(path) as apk:
        html = apk.read("assets/public/index.html").decode("utf-8", "replace")
    match = re.search(r"__PC_APP_BUILD__\s*=\s*(\d+)", html)
    if not match or int(match.group(1)) < 1:
        raise ValueError("APK has no valid embedded PosterChan build")
    return int(match.group(1))


if __name__ == "__main__":
    try:
        print(embedded_build(sys.argv[1]))
    except (IndexError, OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print(f"apk version: {exc}", file=sys.stderr)
        raise SystemExit(1)
