"""The PREVIOUS index-snapshot format (`index.snap`, one npz of every array), kept only so a store written by it can
be IMPORTED once (checkpoint.import_legacy) instead of replaying its whole log. The format that replaced it --
file-backed index, checkpoints that write only what changed -- is checkpoint.py / disk.py."""
from __future__ import annotations

import hashlib
import os

FORMAT = 1
NAME = "index.snap"


def _fingerprint() -> str:
    """The fingerprint the old format recorded (its format number and the tokenizer it indexed with)."""
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256(b"posterchandb-index-%d" % FORMAT)
    for name in ("tsparser.py", "pg_parser_tables.py"):
        with open(os.path.join(here, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:32]
