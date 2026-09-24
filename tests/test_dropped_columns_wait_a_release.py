"""A column is dropped one release AFTER the code stops reading it -- never in the same release.

`_DROPPED_COLUMNS` runs at startup. The release that drops a column therefore shares the database,
for a while, with whatever is still running the previous release: the other roles until they
restart, a node deployed a few minutes later, a second copy booted to check the new code. If the
previous release still reads that column, every query touching the table fails there.

Measured on 2026-09-24: booting the bridge-removal release beside the running one dropped
users.pleroma_access_token (and six more) out from under it -- 2,805 `UndefinedColumn` errors in
eleven minutes, every user lookup 500'd, and reactions and shares failed in the client as "this will
send when you're back online". No test could have seen it, because every test ran ONE release.

So: nothing may be in `_DROPPED_COLUMNS` that the DEPLOYED release (origin/master) still declares.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _dropped():
    import app.database as db
    return list(db._DROPPED_COLUMNS)


def _deployed_models():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "show", "origin/master:app/models.py"],
                              check=True, capture_output=True, text=True, timeout=30).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _declares(models_src, table, column):
    """Whether a model with `__tablename__ = table` declares `column` in that source."""
    for m in re.finditer(r"class \w+\(Base\):(.*?)(?=\nclass |\Z)", models_src, re.S):
        body = m.group(1)
        if re.search(r"__tablename__\s*=\s*['\"]%s['\"]" % re.escape(table), body) and \
                re.search(r"^\s+%s\s*=\s*(Column|mapped_column)\(" % re.escape(column), body, re.M):
            return True
    return False


def test_the_current_model_declares_nothing_it_drops():
    src = (ROOT / "app/models.py").read_text(encoding="utf-8")
    bad = [f"{t}.{c}" for t, c in _dropped() if _declares(src, t, c)]
    assert not bad, f"dropped at startup AND declared by the model: {bad}"


def test_nothing_is_dropped_that_the_deployed_release_still_reads():
    deployed = _deployed_models()
    if deployed is None:
        pytest.skip("no origin/master to compare against")
    bad = [f"{t}.{c}" for t, c in _dropped() if _declares(deployed, t, c)]
    assert not bad, (
        f"these are dropped at startup but the DEPLOYED release still declares them: {bad}. "
        "Stop reading them in this release and drop them in the next one -- dropping them now breaks "
        "every process still running the deployed code against the same database.")


def test_the_check_can_fail():
    """The detector itself, on the shape that shipped: the old model declared the column."""
    src = 'class User(Base):\n    __tablename__ = "users"\n    pleroma_access_token = Column(String(500))\n'
    assert _declares(src, "users", "pleroma_access_token")
    assert not _declares(src, "users", "fedi_only")
    assert not _declares(src, "posts", "pleroma_access_token")
