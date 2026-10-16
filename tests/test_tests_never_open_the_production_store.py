"""A test must never open this machine's PosterChanDB.

2026-10-16 09:36:50: a process running code from a test worktree opened /var/lib/posterchandb/relay -- the LIVE relay's
store on server1 -- because POSTERCHANDB_DIR was unset and data_dir() falls back to the production default. It
imported the legacy index, wrote a checkpoint and run files into the production directory and deleted index.snap.
The data survived; the next one might not.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_suite_and_its_children_get_a_private_store():
    from app.services import posterchandb
    d = posterchandb.data_dir()
    assert os.path.realpath(d) != os.path.realpath(posterchandb.DEFAULT_DIR)
    out = subprocess.run([sys.executable, "-c", "import os; print(os.environ.get('POSTERCHANDB_DIR') or '')"],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    assert out and os.path.realpath(out) != os.path.realpath(posterchandb.DEFAULT_DIR)   # a relay a test spawns


def test_the_production_directory_is_refused_even_when_named(monkeypatch):
    from app.services import posterchandb
    monkeypatch.delenv("POSTERCHANDB_DIR", raising=False)
    with pytest.raises(RuntimeError, match="production"):
        posterchandb.data_dir()
    monkeypatch.setenv("POSTERCHANDB_DIR", posterchandb.DEFAULT_DIR)
    with pytest.raises(RuntimeError, match="production"):
        posterchandb.data_dir()
