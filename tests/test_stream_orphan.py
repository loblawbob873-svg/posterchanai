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
def _restore(tmp_path, monkeypatch):
    """POINT THE PIDFILE AT A TEMP DIR — never at the one the running service owns.

    This used to write and delete `streamserver/mediamtx.pid` in the checkout itself, which on any
    node that is actually SERVING is root-owned by the live MediaMTX: every test in this file then
    died with PermissionError, on the machine where the code matters most. Worse, on a box where the
    file happened to be writable it was reaching into the running deployment's state to run a test.
    A test that behaves differently depending on who owns a file in the working tree cannot tell
    anyone anything about the code — and it is exactly the kind of standing red that teaches people
    to ignore the suite.
    """
    monkeypatch.setattr(stream_service, "_PIDFILE", tmp_path / "mediamtx.pid")
    yield


def test_a_stale_mediamtx_is_killed_before_we_bind_its_ports():
    """A survivor already holds :1935 and :8888, so a new spawn cannot work until it is gone."""
    # A stand-in whose cmdline contains the mediamtx path, so _kill_stale recognises it as ours.
    proc = subprocess.Popen([sys.executable, "-c",
                             f"import time; time.sleep(60)  # {stream_service._STREAM_BIN}"])
    try:
        stream_service._PIDFILE.write_text(str(proc.pid))
        stream_service._kill_stale()
        # THE PROPERTY IS THAT IT DIES, NOT THAT IT DIES IN FIVE SECONDS. `_kill_stale` signals and
        # the kernel reaps whenever it gets round to it — which, on a box already running the rest
        # of the suite, is sometimes longer than the five seconds this allowed. It then failed with
        # the process's returncode ALREADY -9 in the traceback: killed, just not fast enough for the
        # clock. A wall-clock assertion about someone else's scheduler is a test that reports load
        # as a bug, which is how a suite teaches people to ignore it.
        deadline = time.monotonic() + 60
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert proc.poll() is not None, \
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


def test_a_zombie_is_already_dead_for_port_cleanup(tmp_path):
    """kill(pid, 0) says a zombie exists, but it cannot retain MediaMTX's listening sockets."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.monotonic() + 5
    state = ""
    while time.monotonic() < deadline:
        try:
            state = open(f"/proc/{proc.pid}/stat").read().rsplit(") ", 1)[1].split()[0]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        time.sleep(0.02)
    try:
        if state != "Z":
            pytest.skip("host reaped the child before its zombie state could be observed")
        identity = open(f"/proc/{proc.pid}/stat").read().rsplit(") ", 1)[1].split()[19]
        started = time.monotonic()
        assert stream_service._wait_stale_exit(proc.pid, identity, 2.0)
        assert time.monotonic() - started < 0.5
    finally:
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


def _still_running(pid):
    """Is this pid a LIVE process — as opposed to a zombie?

    `os.kill(pid, 0)` cannot tell the difference: a dead-but-unreaped child keeps its pid until
    somebody wait()s for it, and the signal probe succeeds the whole time. Whether anybody does is a
    property of the ENVIRONMENT, not of the thing under test. On a normal host the init system reaps
    orphans immediately, so the probe was right by luck; inside a container whose pid 1 is `sleep
    infinity` — which never calls wait() — nothing ever reaps, and this test reported "the child
    outlived a SIGKILLed parent" on a kernel that had killed it in milliseconds.

    That was measured, not assumed: /proc/<pid>/stat read `Z` a second after the parent died, while
    os.kill kept succeeding. So PDEATHSIG works in a container and the probe was the bug — worth
    stating plainly, because the obvious reading of that failure is "PDEATHSIG does not work in
    Docker", which would have sent the next person to disable the guard the fix depends on.
    """
    try:
        with open("/proc/%d/stat" % pid) as fh:
            # Field 3 is the state. Split after "(comm)" — a process name can contain spaces and
            # parentheses, so splitting the whole line on whitespace picks the wrong field.
            return fh.read().rsplit(") ", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False


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
            if not _still_running(child_pid):
                return              # died with its parent: correct
        pytest.fail("the child outlived a SIGKILLed parent — PDEATHSIG did not take effect")
    finally:
        if parent.poll() is None:
            parent.kill()
