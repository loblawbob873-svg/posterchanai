"""A crashed instrumentation run must leave a stack behind, not three empty artifacts.

Measured on run 34400576969: `AppViewsLaunchSmokeTest` failed, gradle said "Instrumentation run
failed due to Process crashed", and the three artifacts said, between them, nothing.
The HTML report carried a test name and no message (there is no exception when the PROCESS dies),
and the uploaded logcat was filtered to `PosterChan:*` and `TestRunner:*` — so it showed
`started:` with no `finished:` and not one line of cause, because a crash is logged by
AndroidRuntime / chromium / DEBUG. There is no device on the build box (no KVM here), so a gate
that cannot say WHY costs a full CI round trip per guess.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/android_instrumented.sh").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github/workflows/android-emulator.yml").read_text(encoding="utf-8")


def test_the_unfiltered_buffer_is_captured_and_not_only_our_own_tags():
    assert "-s PosterChan:* TestRunner:*" in SCRIPT, "the measuring capture stays"
    full = [line for line in SCRIPT.splitlines() if "logcat -d -v threadtime" in line]
    assert full, "a crash logs under tags we do not own; the unfiltered buffer is the evidence"


def test_the_crash_buffer_is_taken_separately_because_it_survives_the_dead_process():
    assert "logcat -d -b crash" in SCRIPT


def test_every_capture_is_bounded_and_cannot_fail_the_run_that_produced_it():
    """Diagnostics must never be the reason a red build reports a different failure — or hang."""
    # Only the DIAGNOSTIC captures — `adb devices` is a cheap predicate, not a dump.
    for line in SCRIPT.splitlines():
        if "adb logcat" in line or "adb shell" in line:
            assert "timeout --kill-after=" in line, line
    for name in ("logcat-instrumented-full.txt", "logcat-instrumented-crash.txt", "tombstones.txt"):
        block = SCRIPT.split(name, 1)[1].splitlines()[0]
        assert "|| true" in block, f"{name} capture must not be able to fail the step: {block}"


def test_the_evidence_is_uploaded_where_the_report_already_goes():
    """/tmp/pc-androidtest is the androidTest-report artifact, so anything written there ships."""
    assert "/tmp/pc-androidtest" in WORKFLOW
    for name in ("logcat-instrumented-full.txt", "logcat-instrumented-crash.txt"):
        assert f"/tmp/pc-androidtest/{name}" in SCRIPT
