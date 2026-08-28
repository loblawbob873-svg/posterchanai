#!/usr/bin/env python3
"""Fail when Android signing keys or literal signing credentials are tracked."""

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
KEY_SUFFIXES = {".jks", ".keystore", ".p12", ".pfx"}
SKIP = {
    "scripts/check_no_android_signing_secrets.py",
    "tests/test_android_signing_secrets.py",
}
LITERAL_CREDENTIALS = (
    re.compile(r"\b(?:storePassword|keyPassword|keyAlias)\s+[\"'][^\"']+[\"']"),
    re.compile(r"\b(?:srcstorepass|srckeypass|deststorepass|destkeypass)\s+(?![\"']?\$)[^\s\\]+"),
    re.compile(r"\bKEYSTORE_PASSWORD=[^\"'$\s][^\s\\]*"),
)


def tracked_files():
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    )
    return [Path(p.decode()) for p in result.stdout.split(b"\0") if p]


def main():
    problems = []
    for relative in tracked_files():
        name = relative.as_posix()
        if relative.suffix.lower() in KEY_SUFFIXES:
            problems.append(f"tracked private-key container: {name}")
            continue
        if name in SKIP:
            continue
        path = ROOT / relative
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in LITERAL_CREDENTIALS):
                problems.append(f"literal signing credential: {name}:{line_number}")

    if problems:
        print("Android signing-secret guard failed:", file=sys.stderr)
        print("\n".join(f"  - {problem}" for problem in problems), file=sys.stderr)
        return 1
    print("No Android signing keys or literal signing credentials are tracked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
