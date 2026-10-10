#!/usr/bin/env python3
"""Kept for the command line it shipped with: the wave-2 tables of #161 (bots, user_settings, conversations, and
the read-only census of the legacy chat `messages`) migrate through the ONE registry and engine now --
`scripts/migrate_tables_to_nostr.py`, which this only forwards to.

    venv-unified/bin/python scripts/migrate_wave2_tables.py --table user_settings
    venv-unified/bin/python scripts/migrate_wave2_tables.py --table all        # the four wave-2 tables
    venv-unified/bin/python scripts/migrate_tables_to_nostr.py --table bots    # the same, the one script

Output is one JSON report per table: counts, timings and keys -- never a row's content (bot configs hold keys,
settings hold mail passwords, messages are private).

Exit codes: 0 migrated (or already), 1 a copy did not verify (no marker written), 2 could not run (relay or
database unreachable).
"""
import argparse
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def _one_script():
    spec = importlib.util.spec_from_file_location("migrate_tables_to_nostr",
                                                  os.path.join(_HERE, "migrate_tables_to_nostr.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv=None, session_factory=None) -> int:
    from app.services.wave2_migration import MESSAGES, TABLES
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--table", required=True, choices=list(TABLES) + [MESSAGES, "all"])
    args = ap.parse_args(argv)
    names = list(TABLES) + [MESSAGES] if args.table == "all" else [args.table]
    fwd = []
    for n in names:
        fwd += ["--table", n]
    return _one_script().main(fwd, session_factory=session_factory)


if __name__ == "__main__":
    sys.exit(main())
