"""Scratch space on DISK for large media work.

`/tmp` is a tmpfs on the nodes (RAM: 32 GB on server1), so a multi-GB video source streamed "to disk" there is
streamed into memory. Long-video work -- Meme Builder sources and renders, chunked uploads -- goes under
`data/work/` (or $PC_WORK_DIR) instead, on the data disk.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def root() -> str:
    d = os.environ.get("PC_WORK_DIR") or str(_ROOT / "data" / "work")
    os.makedirs(d, exist_ok=True)
    return d


# Every prefix a scratch directory here is made with. sweep() removes only these, and only once they are older
# than any job that could still be using them -- the same allowlist rule as temp_sweep_service for /tmp.
PREFIXES = ("pcmeme-", "pcmemesrc-")
MAX_AGE_S = 12 * 3600          # a 30-minute render's budget is ~2 h (meme_builder_service.render_timeout)


def scratch(prefix: str) -> str:
    """A fresh private directory on the data disk; the caller removes it (sweep() catches what a crash leaves)."""
    if prefix not in PREFIXES:
        raise ValueError("unknown scratch prefix %r -- add it to work_dir.PREFIXES" % prefix)
    return tempfile.mkdtemp(prefix=prefix, dir=root())


def sweep(now: float | None = None, max_age_s: float = MAX_AGE_S) -> int:
    """Remove scratch directories a crashed or killed job left behind. Returns how many were removed."""
    import shutil
    import time
    now = now or time.time()
    gone = 0
    try:
        names = os.listdir(root())
    except OSError:
        return 0
    for name in names:
        if not name.startswith(PREFIXES):
            continue
        path = os.path.join(root(), name)
        try:
            if os.path.isdir(path) and now - os.path.getmtime(path) > max_age_s:
                shutil.rmtree(path, ignore_errors=True)
                gone += 1
        except OSError:
            pass
    return gone
