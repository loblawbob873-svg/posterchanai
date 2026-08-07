"""MediaMTX must not outlive the app that started it.

`_terminate` acts on `_proc`, an in-process handle, so it only ever fires on a CLEAN shutdown. A
SIGKILL, a crash or an OOM leaves MediaMTX running, and the next app process starts with `_proc =
None` and no knowledge of it.

That is not a leak, it is a silent configuration freeze: the survivor keeps serving the config it
loaded at ITS start, so every later change to mediamtx.gen.yml deploys, verifies green, and does
nothing. Found in production — an instance from 13:28 survived several restarts, so `runOnReady`
(the bitrate clamp) never applied and viewers pulled the streamer's full source bitrate off a
residential uplink. Nothing in any log said so, because `logDestinations: [stdout]` was writing into
the dead parent's pipe.

The kill path is the dangerous half: pids are reused, so signalling one without checking what it now
IS would be a far worse bug than the one being fixed.
"""
import os
import subprocess
import sys
import time

import pytest

from app.services import stream_service


@pytest.fixture(autouse=True)
def _restore():
    pid = stream_service._PIDFILE
    had = pid.read_text() if pid.exists() else None
    yield
    if had is None:
        try:
            pid.unlink()
        except FileNotFoundError:
            pass
    else:
        pid.write_text(had)


def test_a_stale_mediamtx_is_killed_before_we_bind_its_ports():
    """A survivor already holds :1935 and :8888, so a new spawn cannot work until it is gone."""
    # A stand-in whose cmdline contains the mediamtx path, so _kill_stale recognises it as ours.
    proc = subprocess.Popen([sys.executable, "-c",
                             f"import time; time.sleep(60)  # {stream_service._STREAM_BIN}"])
    try:
        stream_service._PIDFILE.write_text(str(proc.pid))
        stream_service._kill_stale()
        assert proc.poll() is not None or proc.wait(timeout=5) is not None, \
            "the stale mediamtx survived — the new one cannot bind its ports and the old config lives on"
        assert not stream_service._PIDFILE.exists(), "the pidfile outlived the process it names"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_a_reused_pid_is_never_signalled():
    """The pid in the file may since have become something else entirely.

    Killing whatever inherited it would turn a stream bug into an arbitrary-process-kill bug, so the
    cmdline has to be checked before anything is signalled.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        stream_service._PIDFILE.write_text(str(proc.pid))
        stream_service._kill_stale()
        time.sleep(0.3)
        assert proc.poll() is None, \
            "an unrelated process holding a reused pid was killed — never signal without checking cmdline"
        assert not stream_service._PIDFILE.exists(), "the stale pidfile should still be cleared"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_a_missing_or_junk_pidfile_is_harmless():
    stream_service._PIDFILE.write_text("not-a-pid")
    stream_service._kill_stale()          # must not raise
    try:
        stream_service._PIDFILE.unlink()
    except FileNotFoundError:
        pass
    stream_service._kill_stale()          # no file at all: also fine


def test_the_child_is_asked_to_die_with_the_parent():
    """PDEATHSIG is the defence that actually PREVENTS an orphan; the pidfile only cleans one up.

    Checked at the call site, because a preexec_fn that is defined and never passed to Popen looks
    exactly like a working one.
    """
    import inspect
    src = inspect.getsource(stream_service._spawn)
    assert "preexec_fn=_pdeathsig" in src, \
        "mediamtx is spawned without PDEATHSIG — a hard stop of the app orphans it again"
    assert "_kill_stale()" in src, "a survivor from a previous process is not cleared before spawning"
    assert src.index("_kill_stale()") < src.index("subprocess.Popen"), \
        "the stale instance must be gone BEFORE we try to bind the same ports"


def test_pdeathsig_actually_fires_in_a_child():
    """Run the real hook in a forked child and confirm the kernel honours it.

    Asserting the source contains PR_SET_PDEATHSIG proves nothing about whether the ctypes call
    works on this libc.
    """
    if not os.path.exists("/proc/self/status"):
        pytest.skip("needs Linux")
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time;"
         "sys.path.insert(0, %r);" % os.getcwd() +
         "from app.services.stream_service import _pdeathsig;"
         "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
         " preexec_fn=_pdeathsig);"
         "print(p.pid, flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True)
    try:
        child_pid = int(parent.stdout.readline().strip())
        parent.kill()               # hard stop, exactly like the failure this guards
        parent.wait(timeout=5)
        for _ in range(50):
            time.sleep(0.1)
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return              # died with its parent: correct
        pytest.fail("the child outlived a SIGKILLed parent — PDEATHSIG did not take effect")
    finally:
        if parent.poll() is None:
            parent.kill()
