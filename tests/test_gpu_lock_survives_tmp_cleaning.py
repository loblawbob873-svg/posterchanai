"""The GPU lock still works after systemd-tmpfiles deletes its directory.

nas.lan, 2026-10-05: systemd-tmpfiles-clean (`q /tmp 1777 root root 10d`) ran at 12:23 and removed
/tmp/posterchanai_locks -- the lock file is flocked but never written, so it aged past ten days. The
directory was only created at import, so every later AI request logged "waiting for file lock..." and
hung until its timeout (an AI Chat message sat unanswered until the directory was recreated by hand).
"""
import os
import shutil
import time

from app.services import locks


def test_a_deleted_lock_directory_is_recreated_on_the_next_acquire(tmp_path, monkeypatch):
    lock_dir = tmp_path / "posterchanai_locks"
    lock_file = str(lock_dir / "gpu.lock")
    lock_dir.mkdir()
    fd = locks._try_acquire_file_lock(lock_file)
    assert fd is not None
    os.close(fd)
    shutil.rmtree(lock_dir)                       # what the /tmp cleaner did
    fd = locks._try_acquire_file_lock(lock_file)
    assert fd is not None, "the lock could not be taken once its directory was cleaned away"
    os.close(fd)


def test_taking_the_lock_keeps_the_file_young(tmp_path):
    lock_file = str(tmp_path / "gpu.lock")
    open(lock_file, "w").close()
    old = time.time() - 30 * 86400
    os.utime(lock_file, (old, old))               # untouched for a month: next in line for the cleaner
    fd = locks._try_acquire_file_lock(lock_file)
    assert fd is not None
    os.close(fd)
    assert time.time() - os.stat(lock_file).st_mtime < 60, "a lock in use still looks abandoned to tmpfiles"
