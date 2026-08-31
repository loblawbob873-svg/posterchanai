#!/usr/bin/env bash
# Run the instrumented tests on the booted emulator.
#
# ITS OWN FILE because the workflow's `script:` block is handed to `sh` (dash on GitHub's image), and
# a multi-line `if … fi` written there parses as "Syntax error: end of file unexpected". That is not
# hypothetical: it failed every emulator run from the day the instrumented tests were added, so the
# tests — the whole point of having a device in the loop — never executed once. A file with a
# shebang cannot be mis-parsed by whichever shell the runner happens to use.
set -uo pipefail

# A DEVICE THAT NEVER CAME UP IS NOT A TEST RESULT.
#
# This job has gone red for two unrelated reasons in one evening: once because a test genuinely
# failed, and once because the emulator died before a single test ran and gradle said
# `com.android.builder.testing.api.DeviceException: No connected devices!`. Both landed as an
# identical red build, and a signal that is red for infrastructure is a signal people learn to
# re-run without reading — which is precisely how the real failure underneath gets missed.
#
# So this script now answers three things, not two, the same way checkall.py already does locally:
# 0 the device ran the tests and they passed, 1 the device ran them and something failed,
# 2 THE TESTS DID NOT RUN. Exit 2 is never reported as a pass — it is a loud annotation plus a line
# in the job summary saying nothing was verified on a device.
device_present() {
  adb devices 2>/dev/null | awk 'NR>1 && $2=="device" { found = 1 } END { exit !found }'
}
skip() {
  echo "::warning title=Instrumented tests DID NOT RUN::$1"
  { echo "### :warning: Instrumented tests did not run"; echo; echo "$1"; echo;
    echo "This is **not** a pass: nothing was verified on a device."; } \
    >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
  exit 2
}

if ! find mobile/android/app/src/androidTest \( -name '*.java' -o -name '*.kt' \) 2>/dev/null | grep -q .; then
  echo "::warning title=No instrumented tests::mobile/android/app/src/androidTest has no sources — nothing was tested ON the device."
  exit 0
fi

cd mobile/android || exit 1
# Asked BEFORE gradle, because the clearest evidence is the simplest: no device attached means the
# emulator never booted or has already gone, and every second spent building is spent for nothing.
device_present || skip "no emulator was attached when the instrumented tests were due to start (adb devices lists none)."

# `:app:` AND NOT THE ROOT TASK, and that colon is the whole difference between a job that reports
# what a device did and one that never gets to ask.
#
# Bare `connectedDebugAndroidTest` fans out to EVERY subproject, and the subprojects here are the
# Capacitor plugins under node_modules. One of them — send-intent — declares `minSdkVersion 22`,
# which the manifest merger refuses against capacitor-android's 23 for the androidTest variant only.
# So `:send-intent:processDebugAndroidTestManifest` FAILED, the gradle invocation exited non-zero,
# and the step went red — AFTER `:app:connectedDebugAndroidTest` had already run all 34 tests on the
# device and passed every one. A red job whose real answer was green is worse than a red job: it was
# read as "the device tests are still broken" for as long as it stood, so the icon fix underneath it
# was reported as unverified when the device had in fact verified it.
#
# None of those plugin modules has a single androidTest source (every one logs NO-SOURCE), so there
# is nothing being skipped here — only a manifest merge for tests that do not exist.
# Kept for the post-mortem below: the distinguishing sentence is gradle's, and it is only on stdout.
./gradlew :app:connectedDebugAndroidTest --console=plain 2>&1 | tee /tmp/pc-instrumented.log
rc=${PIPESTATUS[0]}
# The HTML/XML report is the only place per-test failures are legible; publish it either way.
mkdir -p /tmp/pc-androidtest
cp -r app/build/reports/androidTests/connected/. /tmp/pc-androidtest/ 2>/dev/null || true
cp -r app/build/outputs/androidTest-results/connected/. /tmp/pc-androidtest/ 2>/dev/null || true
# AND THE LOGCAT THE TESTS THEMSELVES WROTE. A device test can MEASURE something there is no
# assertion for — whether an OEM ships the system widget picker, how many widget providers the image
# has — and the XML report carries only failures. Without this the only way to get a fact off the
# device was to fail a test on purpose.
timeout --kill-after=5s 20s adb logcat -d -s PosterChan:* TestRunner:* \
  > /tmp/pc-androidtest/logcat-instrumented.txt 2>/dev/null || true
cp /tmp/pc-instrumented.log /tmp/pc-androidtest/ 2>/dev/null || true

# THE POST-MORTEM. Reports are copied first so a skip still publishes whatever the device produced.
# Only two shapes count as "did not run", and both are about the DEVICE, never about a test:
# gradle's own sentence, and an emulator that is no longer attached now that it is over.
if [ $rc -ne 0 ]; then
  if grep -q "No connected devices" /tmp/pc-instrumented.log 2>/dev/null; then
    skip "the emulator was gone before any test executed (gradle: \"No connected devices!\")."
  fi
  device_present || skip "the emulator disappeared partway through the run; the results are not a verdict on the code."
fi
exit $rc
