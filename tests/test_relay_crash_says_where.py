"""A relay process that dies on a fatal signal prints its Python stack into its log.

The PosterChanOS local relay segfaulted on a desktop (2026-10-13, after 6h39m) and left nothing but "dumped core"
in the journal -- no Python frame, and no debugger on the box to read the core with. This starts the SHIPPED
`relay_main.py --os-mode`, sends it SIGSEGV, and requires the log to name the crash and the threads' stacks.

Run: venv-unified/bin/python -m pytest tests/test_relay_crash_says_where.py -q
"""
import os
import resource
import shutil
import signal
import time

from tests import os_relay_harness as H


def test_a_segfaulting_local_relay_logs_where_it_was():
    tmp = H.tmpdir()
    relay = H.LocalRelay(tmp)
    try:
        relay.start()
        # No core file: this crash is deliberate, and on a host whose core_pattern writes to the working
        # directory a core of the relay (~150 MB) lands in the checkout -- two of them were committed once.
        resource.prlimit(relay.proc.pid, resource.RLIMIT_CORE, (0, 0))
        os.kill(relay.proc.pid, signal.SIGSEGV)
        relay.proc.wait(timeout=20)
        time.sleep(0.2)
        log = relay.output()
    finally:
        relay.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    assert "Fatal Python error: Segmentation fault" in log, log[-2000:]
    assert "Thread 0x" in log or "Current thread" in log, ("no Python stack in the log", log[-2000:])
