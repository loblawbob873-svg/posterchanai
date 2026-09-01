"""A CRASH IS OURS OR IT IS NOT — the device check could not tell, and failed a build over Google's.

`crash_scan` in scripts/android_device_checks.sh decides whether the app crashed during a phase. It
read:

    if grep -q "FATAL EXCEPTION" logcat && grep -q "$PKG" logcat; then fail "the app crashed"

Two independent greps over the same file, which is not the same question as "did OUR app crash".
Our package appears in every logcat this script ever captures — it is the app under test, its
activity names, its services and its own log lines are all in there — so the second grep is always
true and the condition collapsed to **"did anything on this emulator crash"**.

It then failed a real CI run on this:

    FATAL EXCEPTION: pool-2-thread-1
    Process: com.google.android.permissioncontroller, PID: 836
    java.lang.NullPointerException
        at com.android.permissioncontroller.permission.utils.KotlinUtilsKt.getInitializedValue

Google's own permission UI, on Google's own emulator image, throwing inside its own Kotlin — and the
run said "FAIL: the app crashed during: launch". Nothing about PosterChan was wrong, and 84 of 84
instrumentation tests passed in the same run.

That is worse than a flake, because of what it teaches: a device check that is usually somebody
else's crash is a device check people stop reading, and the next time it is ours nobody will look.

The scan is attributed now — the `Process:` line belonging to the SAME crash block has to name our
package. These tests run the real script's logic against real logcat shapes, including the exact
trace from the failing run.
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/android_device_checks.sh"
PKG = "place.poster.app"

pytestmark = pytest.mark.skipif(shutil.which("awk") is None, reason="awk not available")


def _awk_condition():
    """The awk program crash_scan uses to decide, lifted from the shipped script."""
    text = SCRIPT.read_text(encoding="utf-8")
    marker = "if awk -v pkg=\"$PKG\" '"
    start = text.index(marker) + len(marker)
    # The program ends at the closing quote that precedes the logcat path argument.
    end = text.index("\"$OUT/pc-logcat-$1.txt\"; then", start)
    return text[start:text.rindex("'", start, end)]


def crashed(logcat: str) -> bool:
    """True when the script would call this OUR crash."""
    program = _awk_condition()
    done = subprocess.run(["awk", "-v", f"pkg={PKG}", program],
                          input=logcat, text=True, capture_output=True, timeout=30)
    assert done.returncode in (0, 1), done.stderr
    return done.returncode == 0


#: The exact crash that failed the run, trimmed to the shape awk sees.
PERMISSIONCONTROLLER = textwrap.dedent("""\
    09-01 01:17:58.036   836   876 E AndroidRuntime: FATAL EXCEPTION: pool-2-thread-1
    09-01 01:17:58.036   836   876 E AndroidRuntime: Process: com.google.android.permissioncontroller, PID: 836
    09-01 01:17:58.036   836   876 E AndroidRuntime: java.lang.NullPointerException
    09-01 01:17:58.036   836   876 E AndroidRuntime: \tat com.android.permissioncontroller.permission.utils.KotlinUtilsKt.getInitializedValue(KotlinUtils.kt:1549)
    """)

OURS = textwrap.dedent(f"""\
    09-01 01:20:03.100  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main
    09-01 01:20:03.100  1234  1234 E AndroidRuntime: Process: {PKG}, PID: 1234
    09-01 01:20:03.100  1234  1234 E AndroidRuntime: java.lang.IllegalStateException: boom
    09-01 01:20:03.100  1234  1234 E AndroidRuntime: \tat place.poster.app.home.HomeActivity.onCreate(HomeActivity.java:1)
    """)

#: Our package is all over an ordinary logcat even when nothing of ours has crashed. This is the
#: noise that made the old two-grep condition always true.
OUR_ORDINARY_NOISE = textwrap.dedent(f"""\
    09-01 01:17:50.000  1234  1234 I ActivityManager: Start proc 1234:{PKG}/u0a999 for activity
    09-01 01:17:51.000  1234  1234 D PosterChan: launcher drew 48 entries
    09-01 01:17:52.000  1234  1234 I {PKG}: sync sweep finished
    """)


def test_somebody_elses_crash_is_not_ours():
    """THE FAILING RUN, exactly. A system app crashing beside us is not our build breaking."""
    assert crashed(PERMISSIONCONTROLLER + OUR_ORDINARY_NOISE) is False, (
        "a crash in com.google.android.permissioncontroller is still reported as ours — this is "
        "the false failure the whole file exists for")


def test_our_own_crash_is_still_caught():
    """The check must not have been softened into uselessness, which is the obvious wrong fix."""
    assert crashed(OURS + OUR_ORDINARY_NOISE) is True


def test_our_crash_is_found_even_when_another_app_crashed_first():
    """Order must not matter: an emulator that throws its own exception before ours must not hide
    ours behind it."""
    assert crashed(PERMISSIONCONTROLLER + OURS) is True
    assert crashed(OURS + PERMISSIONCONTROLLER) is True


def test_a_clean_log_full_of_our_name_is_not_a_crash():
    """The specific mechanism of the bug: our package in the log is not evidence of anything."""
    assert crashed(OUR_ORDINARY_NOISE) is False


def test_an_empty_log_is_not_a_crash():
    assert crashed("") is False


def test_a_similarly_named_package_is_not_us():
    """`Process: place.poster.app,` — the trailing comma is load-bearing, or a hypothetical
    `place.poster.apple` would be read as this app crashing."""
    impostor = PERMISSIONCONTROLLER.replace("com.google.android.permissioncontroller",
                                            PKG + "le")
    assert crashed(impostor) is False


def test_a_crash_with_no_process_line_is_not_attributed_to_us():
    """A truncated or interleaved trace tells us nothing about whose it was, and guessing "ours"
    puts us straight back to failing on other people's noise."""
    assert crashed("E AndroidRuntime: FATAL EXCEPTION: main\nE AndroidRuntime: java.lang.Error\n") is False


def test_the_shipped_script_still_parses():
    """The condition lives in a shell script, so a syntax error here is a device-check job that
    dies before it checks anything."""
    done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_the_two_independent_greps_are_gone():
    """Names the exact regression: two facts about one file are not one fact about one crash."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'grep -q "FATAL EXCEPTION" "$OUT/pc-logcat-$1.txt" && grep -q "$PKG"' not in text, (
        "crash_scan is back to asking 'did anything crash' AND 'is our name in the file'")
    assert 'Process: " pkg ","' in text or 'Process: " pkg ",' in text, (
        "the crash is no longer attributed by its Process: line")
