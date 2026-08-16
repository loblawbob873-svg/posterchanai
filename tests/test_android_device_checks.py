"""The device check script, RUN — against a stub adb, on this machine.

This box cannot boot an emulator (its firmware has SVM off, so kvm_amd will not load), which is the
whole reason the real checks live on GitHub's runners. What it CAN do is run the script's own logic,
and that is worth doing for one specific reason: the first version of that script hung for
fifty-five minutes inside a single `adb logcat -d` and was cancelled by the job timeout. Nothing in
this repo could have caught that, because every Android guard here matches TEXT in a source file and
the text was all perfectly correct.

So these tests give the script an `adb` that behaves badly on purpose — one that hangs, one that
fails, one that reports a crash — and assert what the script DOES. Two properties matter more than
the rest and each is verified to fail without its guard:

  * a hang becomes a FAILING check in seconds, not a job timeout;
  * a log that could NOT be read is never reported as "no crash". A false green on a crash check is
    worse than no check at all.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "android_device_checks.sh"


def _stub_adb(tmp_path: Path, *, logcat_body: str = "", logcat_mode: str = "ok",
              crashlog: str = "", pidof: int = 0) -> Path:
    """An `adb` that answers everything blandly except the one behaviour under test.

    logcat_mode: ok | hang | error
    """
    body = f"""#!/usr/bin/env bash
args="$*"
case "$args" in
  *"logcat -c"*) exit 0 ;;
  *"logcat -d"*)
      case "{logcat_mode}" in
        hang)  sleep 120; exit 0 ;;
        error) echo "adb: device offline" >&2; exit 1 ;;
        *)     cat <<'PC_LOG'
{logcat_body}
PC_LOG
               exit 0 ;;
      esac ;;
  *"run-as"*)
      printf '%s' {crashlog!r}
      exit 0 ;;
  *pidof*) exit {pidof} ;;
esac
exit 0
"""
    p = tmp_path / "adb"
    p.write_text(body)
    p.chmod(0o755)
    return p


def _run(tmp_path: Path, adb: Path, **env_extra):
    env = dict(os.environ)
    env.update(
        PC_ADB=str(adb),
        PC_OUT=str(tmp_path),
        PC_APK=str(tmp_path / "app-debug.apk"),
        PC_SETTLE="0",          # the cycle's real seconds are not the thing under test
        PC_LOGCAT_SECS="2",
        PC_ADB_SECS="5",
    )
    env.update(env_extra)
    (tmp_path / "app-debug.apk").write_text("")
    started = time.time()
    proc = subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )
    return proc, time.time() - started


def test_a_clean_device_passes():
    """The happy path has to actually pass, or every other assertion here is vacuous."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        adb = _stub_adb(tmp, logcat_body="I/PosterChan: hello\nD/Something: fine")
        proc, _ = _run(tmp, adb)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "All device checks passed." in proc.stdout
        assert proc.stdout.count("ok: no crash during") == 4


def test_a_hanging_logcat_fails_fast_instead_of_hanging_the_job():
    """THE BUG THIS FILE EXISTS FOR.

    `adb logcat -d` on a freshly booted AVD (which logs at *:V) can be handed a buffer that refills
    faster than it drains and simply never return. Unbounded, that is a fifty-five minute silence
    and a cancelled job that names no step. Bounded, it is a red check with a sentence.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        adb = _stub_adb(tmp, logcat_mode="hang")
        proc, elapsed = _run(tmp, adb)
        assert proc.returncode == 1, proc.stdout
        assert "logcat did not finish within" in proc.stdout
        # The whole point: it ended in seconds. The stub would sleep two minutes per call.
        assert elapsed < 60, f"took {elapsed:.0f}s — the timeout is not bounding the call"


def test_a_log_that_could_not_be_read_is_never_reported_as_no_crash():
    """An unreadable probe reporting health is the /logs board's oldest lesson, restated.

    "I could not look" and "I looked and it was clean" are different answers, and collapsing them
    makes the check report green on exactly the runs where it knows least.
    """
    import tempfile
    for mode, expected in (("hang", "logcat did not finish"), ("error", "logcat could not be read")):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adb = _stub_adb(tmp, logcat_mode=mode)
            proc, _ = _run(tmp, adb)
            assert proc.returncode == 1, proc.stdout
            assert expected in proc.stdout
            assert "ok: no crash during" not in proc.stdout, (
                f"an unreadable log ({mode}) was reported as a clean one"
            )


def test_a_fatal_exception_fails_the_run_and_prints_the_trace():
    import tempfile
    log = (
        "I/ActivityManager: Start proc place.poster.app\n"
        "E/AndroidRuntime: FATAL EXCEPTION: main\n"
        "E/AndroidRuntime: Process: place.poster.app, PID: 4242\n"
        "E/AndroidRuntime: java.lang.NullPointerException\n"
        "E/AndroidRuntime: \tat place.poster.app.sync.FolderSyncPlugin.handOver(FolderSyncPlugin.java:430)\n"
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        adb = _stub_adb(tmp, logcat_body=log)
        proc, _ = _run(tmp, adb)
        assert proc.returncode == 1, proc.stdout
        assert "the app crashed during: launch" in proc.stdout
        assert "FolderSyncPlugin.handOver" in proc.stdout, "the trace itself must be printed"


def test_an_anr_is_a_crash_by_another_name():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        adb = _stub_adb(tmp, logcat_body="E/ActivityManager: ANR in place.poster.app")
        proc, _ = _run(tmp, adb)
        assert proc.returncode == 1
        assert "ANR in place.poster.app" in proc.stdout


def test_the_apps_own_crash_log_is_read_and_fails_the_run():
    """The APK records its own crashes now (CrashLog + PosterChanApp), because a phone in the field
    has no cable. Reading it here as well is what keeps the two honest: a crash logcat missed still
    lands in the file, and a file that stays empty while logcat shows a FATAL EXCEPTION means the
    handler is not installed on the path that died."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        adb = _stub_adb(
            tmp,
            logcat_body="I/PosterChan: nothing to see",
            crashlog="PosterChan crash · 1.0.517 · 2026-08-16\n"
                     "thread: main · app backgrounded · sync service up\n"
                     "java.lang.IllegalStateException: not allowed\n",
        )
        proc, _ = _run(tmp, adb)
        assert proc.returncode == 1, proc.stdout
        assert "the app recorded a crash of its own" in proc.stdout
        assert "IllegalStateException" in proc.stdout


def test_every_adb_call_in_the_script_is_bounded():
    """The wiring half. A new adb call added without a timeout re-opens the exact hole above, and it
    would look completely ordinary in review."""
    for n, line in enumerate(SCRIPT.read_text().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or '"$ADB"' not in stripped:
            continue
        assert "timeout" in stripped, f"{SCRIPT.name}:{n} calls adb without a timeout: {stripped}"


def test_editing_the_check_script_actually_runs_the_check():
    """The script IS the check, so it has to be one of the paths that triggers the workflow.

    Without it the only way to exercise a fix to these checks is to edit the workflow file in the
    same commit — which is how the first version happened to ship, and the next fix would have gone
    to a repo that quietly did not run it. A check nothing triggers is indistinguishable from a
    passing one.
    """
    wf = (ROOT / ".github" / "workflows" / "android-emulator.yml").read_text()
    assert "scripts/android_device_checks.sh" in wf


@pytest.mark.parametrize("workflow_key,expected", [("timeout-minutes: 35", True)])
def test_the_job_timeout_is_no_longer_an_hour(workflow_key, expected):
    """An hour is not a safety net, it is a bill. The script bounds its own calls now, so the job
    timeout is the last line of defence rather than the only one."""
    wf = (ROOT / ".github" / "workflows" / "android-emulator.yml").read_text()
    assert (workflow_key in wf) is expected
    assert "timeout-minutes: 60" not in wf
