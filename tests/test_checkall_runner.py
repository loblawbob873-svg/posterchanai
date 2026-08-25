import pathlib
import sys
import tempfile
import time

from scripts import checkall


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
