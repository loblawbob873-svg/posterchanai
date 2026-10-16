"""PosterChanDB — the relay's RAM-first event store. See docs/POSTERCHANDB.md."""
import os
import sys

from .codec import Codec  # noqa: F401
from .store import Store  # noqa: F401

DEFAULT_DIR = "/var/lib/posterchandb"


def data_dir() -> str:
    """$POSTERCHANDB_DIR (the run script sources data/secrets.env; Docker sets it), else /var/lib/posterchandb —
    the same rule scripts/install/posterchandb.sh uses, so the installer prepares the directory the store opens."""
    d = os.environ.get("POSTERCHANDB_DIR") or DEFAULT_DIR
    # 2026-10-16 09:36: something run from a test tree opened THIS machine's production store (it fell back to the
    # default because nothing set the variable), imported its index and wrote a checkpoint into it. A test never gets
    # the production directory: tests/conftest.py points the variable at the run's private temp dir, and this refuses
    # what slips past it (a test that clears the environment, a helper imported outside that conftest).
    if "pytest" in sys.modules and os.path.realpath(d) == os.path.realpath(DEFAULT_DIR):
        raise RuntimeError("PosterChanDB: refusing the production directory %s under pytest -- set POSTERCHANDB_DIR"
                           % DEFAULT_DIR)
    return d
