#!/usr/bin/env python3
"""Nothing on this node that holds a credential may be readable by other local accounts.

    venv-unified/bin/python scripts/check_secrets_not_world_readable.py

WHY THIS IS A CHECK AND NOT A TEST. `tests/conftest.py` redirects `settings_store._LOCAL_PATH` to a
tmp dir for every test — correctly, because a test must not touch this host — so a pytest assertion
about the real file can only ever skip, and it did, on a node where the file was 0644 at that moment.
Only something that looks at the machine can answer this.

WHAT IT FOUND THE DAY IT WAS WRITTEN, on a host that also carries the `pleroma`, `linkbot`, `misskey`
and `aikey` accounts:

  * /var/lib/posterchanai/local_settings.json  0644 — holds `nostr_relay_pg_dsn`, i.e. the PASSWORD
    for the relay database: every user's events, encrypted DMs, notes, calendars and contacts.
  * /var/lib/posterchanai/posterchanai.db      0644 — holds the admin's bcrypt password hash, an
    offline cracking target.

Exit 0 = every candidate is private, 1 = something is readable it should not be, 2 = could not run.
"""
import os
import stat
import sys
from pathlib import Path

# Files that hold, or have held, a credential. A glob rather than a list where the name varies.
CANDIDATES = [
    "local_settings.json",
    "posterchanai.db",
    "app.db",
    "db.sqlite3",
    "monero_wallet_spend.sqlite3",
    "keys.json",
]


def data_dir() -> Path:
    return Path(os.environ.get("POSTERCHANAI_DATA", "/var/lib/posterchanai"))


def main() -> int:
    root = data_dir()
    if not root.is_dir():
        print(f"SKIP  no data directory at {root} — nothing to check")
        return 2
    problems, checked = [], 0
    for name in CANDIDATES:
        for path in sorted(root.glob(name + "*")):
            if not path.is_file() or path.name.endswith(".lock"):
                continue
            checked += 1
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                problems.append(f"{path} is {oct(mode)} — group/other can read it")
    print(f"checked {checked} credential-bearing file(s) under {root}")
    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  - {p}")
        print("\n  chmod 0600 them, and fix whatever WRITES them — a chmod by hand was measured "
              "back at 0644 within seconds, because the app rewrites the file on every local "
              "settings change.")
        return 1
    print("OK  every credential-bearing file is private to its owner")
    return 0


if __name__ == "__main__":
    sys.exit(main())
