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


def mkdtemp(prefix: str) -> str:
    """A fresh private directory on the data disk; the caller removes it."""
    return tempfile.mkdtemp(prefix=prefix, dir=root())
