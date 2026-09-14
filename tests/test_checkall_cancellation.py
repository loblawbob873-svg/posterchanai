"""Ctrl-C stops owned checks and their children without starting queued work."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="Linux process inspection")
def test_interrupt_cancels_queued_checks_and_reaps_running_children(tmp_path):
    first = tmp_path / "first.py"
    pids = tmp_path / "owned.pids"
    queued = tmp_path / "queued.started"
    first.write_text(
        "import os,pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    second = tmp_path / "second.py"
    second.write_text(f"from pathlib import Path; Path({str(queued)!r}).touch()\n")
    code = f"""
from pathlib import Path
from scripts import checkall
import sys
checkall.ROOT=Path({str(tmp_path)!r})
checkall.SUITES=[]
checkall.have_chrome=lambda: '/fixture/chrome'
checkall.have_node=lambda: '/fixture/node'
checkall.discover=lambda: [
    dict(name='first',path=Path({str(first)!r}),group='ui',secs=60,registered=True),
    dict(name='second',path=Path({str(second)!r}),group='ui',secs=60,registered=True),
]
sys.argv=['checkall','--jobs','1','--tmp',{str(tmp_path / 'logs')!r}]
raise SystemExit(checkall.main())
"""
    runner = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True)
    owned = []
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pids.exists() and len(pids.read_text().split()) == 2:
                owned = list(map(int, pids.read_text().split()))
                break
            assert runner.poll() is None, runner.communicate()
            time.sleep(0.02)
        assert owned, "first check never started"
        runner.send_signal(signal.SIGINT)
        try:
            stdout, stderr = runner.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("Ctrl-C left the runner waiting for queued checks")
        assert runner.returncode == 130, stdout + stderr
        assert not queued.exists(), "Ctrl-C allowed a queued check to start"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in owned):
            time.sleep(0.02)
        assert not any(Path(f"/proc/{pid}").exists() for pid in owned), "owned check children leaked"
    finally:
        # The intentionally red version must not leave sleeping fixture processes behind.
        for pgid in [runner.pid] + owned[:1]:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        runner.communicate(timeout=5)
