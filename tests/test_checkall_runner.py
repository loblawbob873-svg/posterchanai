import pathlib
import os
import sys
import tempfile
import time

from scripts import checkall


def test_os_back_is_an_explicit_live_check():
    """The OS Back driver consumes real repos/issues and must never hide in the local UI group."""
    jobs = {job["name"]: job for job in checkall.discover()}
    assert jobs["check_os_back"]["registered"] is True
    assert jobs["check_os_back"]["group"] == "live"


def test_finished_check_does_not_wait_for_a_grandchild_holding_stdout():
    """A browser inherited the old PIPE and froze ./test.sh after pytest had already exited."""
    code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)']); "
        "print('parent finished')"
    )
    with tempfile.TemporaryDirectory() as d:
        started = time.monotonic()
        rc, out = checkall._captured(
            [sys.executable, "-c", code], pathlib.Path(d), None, 5, pathlib.Path(d) / "out.log"
        )
    assert rc == 0
    assert "parent finished" in out
    assert time.monotonic() - started < 2


def test_timeout_stops_the_job_and_reports_it():
    with tempfile.TemporaryDirectory() as d:
        rc, out = checkall._captured(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            pathlib.Path(d), None, 0.1, pathlib.Path(d) / "timeout.log"
        )
    assert rc == 124
    assert "killed after 0.1s" in out


def test_finished_check_reaps_its_browser_process_group(tmp_path):
    """A driver returning after terminate() must not leave Chromium for the next check."""
    pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
        "print('driver returned')"
    )
    rc, out = checkall._captured([sys.executable, "-c", code], tmp_path, None, 5,
                                 tmp_path / "reap.log")
    child = int(pid_file.read_text())
    assert rc == 0 and "driver returned" in out
    for _ in range(50):
        if not pathlib.Path(f"/proc/{child}").exists():
            break
        time.sleep(0.02)
    assert not pathlib.Path(f"/proc/{child}").exists()


def test_reruns_in_one_log_directory_get_distinct_browser_profiles(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(checkall, "_captured",
                        lambda argv, cwd, env, timeout, output: (seen.append(env["PC_CHECK_PROFILE"])
                                                                 or (0, "OK")))
    job = {"name": "check_probe", "path": tmp_path / "probe.py", "group": "ui",
           "secs": 1, "env": {}}
    checkall.run_one(job, None, tmp_path, 3)
    checkall.run_one(job, None, tmp_path, 4)
    assert seen[0] != seen[1]
    assert all(str(tmp_path / "profiles") in profile for profile in seen)
    assert all(str(os.getpid()) in profile for profile in seen)
