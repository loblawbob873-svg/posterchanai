"""PosterChanDB — the relay's RAM-first event store. See docs/POSTERCHANDB.md."""
import os

from .codec import Codec  # noqa: F401
from .store import Store  # noqa: F401

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def data_dir() -> str:
    """$POSTERCHANDB_DIR (the run script sources data/secrets.env; Docker sets it), else <repo>/data/posterchandb —
    the same rule scripts/install/posterchandb.sh uses, so the installer prepares the directory the store opens."""
    return os.environ.get("POSTERCHANDB_DIR") or os.path.join(_ROOT, "data", "posterchandb")
