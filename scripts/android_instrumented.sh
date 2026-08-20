#!/usr/bin/env bash
# Run the instrumented tests on the booted emulator.
#
# ITS OWN FILE because the workflow's `script:` block is handed to `sh` (dash on GitHub's image), and
# a multi-line `if … fi` written there parses as "Syntax error: end of file unexpected". That is not
# hypothetical: it failed every emulator run from the day the instrumented tests were added, so the
# tests — the whole point of having a device in the loop — never executed once. A file with a
# shebang cannot be mis-parsed by whichever shell the runner happens to use.
set -uo pipefail

if ! find mobile/android/app/src/androidTest \( -name '*.java' -o -name '*.kt' \) 2>/dev/null | grep -q .; then
  echo "::warning title=No instrumented tests::mobile/android/app/src/androidTest has no sources — nothing was tested ON the device."
  exit 0
fi

cd mobile/android || exit 1
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
./gradlew :app:connectedDebugAndroidTest --console=plain
rc=$?
# The HTML/XML report is the only place per-test failures are legible; publish it either way.
mkdir -p /tmp/pc-androidtest
cp -r app/build/reports/androidTests/connected/. /tmp/pc-androidtest/ 2>/dev/null || true
cp -r app/build/outputs/androidTest-results/connected/. /tmp/pc-androidtest/ 2>/dev/null || true
# AND THE LOGCAT THE TESTS THEMSELVES WROTE. A device test can MEASURE something there is no
# assertion for — whether an OEM ships the system widget picker, how many widget providers the image
# has — and the XML report carries only failures. Without this the only way to get a fact off the
# device was to fail a test on purpose.
adb logcat -d -s PosterChan:* TestRunner:* > /tmp/pc-androidtest/logcat-instrumented.txt 2>/dev/null || true
exit $rc
