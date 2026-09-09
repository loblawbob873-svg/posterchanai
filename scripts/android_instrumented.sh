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

# The instrumentation boot is independent of the lifecycle boot. Establish the same
# explicit granted-runtime-permission fixture that lifecycle's install -r -g supplied before
# these gates were split. Otherwise Dialer's real permission dialog prevents ActivityScenario
# resume, and restoring the SMS role revokes role-only permissions and kills the test process.
# This changes only the disposable test installation; role/provider assertions remain real.
APK=$(find app/build/outputs/apk -path '*debug*' -name '*.apk' ! -name '*androidTest*' | head -1)
[ -n "$APK" ] || { echo "no debug APK built for instrumentation"; exit 1; }
timeout --kill-after=2s 120s adb install -r -g "$APK" || { echo "instrumentation install failed"; exit 1; }

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
# AND THE UNFILTERED BUFFER, BECAUSE A CRASH DOES NOT LOG UNDER OUR TAGS.
#
# The tag filter above is right for a test that MEASURES something and wrong for the failure that
# actually happens: when the app process dies, gradle says "Instrumentation run failed due to
# Process crashed", the HTML report carries a test name and no message, and the filtered logcat
# shows `started:` with no `finished:` — three artifacts and not one line of cause, because the
# stack is logged by AndroidRuntime/chromium/DEBUG. Measured on run 34400576969: 112 tests, one
# failure, zero evidence. The crash buffer is separate from main and survives a died process, so
# both are taken.
timeout --kill-after=5s 30s adb logcat -d -v threadtime \
  > /tmp/pc-androidtest/logcat-instrumented-full.txt 2>/dev/null || true
timeout --kill-after=5s 20s adb logcat -d -b crash -v threadtime \
  > /tmp/pc-androidtest/logcat-instrumented-crash.txt 2>/dev/null || true
# A native crash writes a tombstone rather than a Java stack; a WebView renderer death is one.
timeout --kill-after=5s 20s adb shell "ls -t /data/tombstones 2>/dev/null | head -3" \
  > /tmp/pc-androidtest/tombstones.txt 2>/dev/null || true
cp /tmp/pc-instrumented.log /tmp/pc-androidtest/ 2>/dev/null || true

# THE POST-MORTEM. Reports are copied first so a skip still publishes whatever the device produced.
# Only two shapes count as "did not run", and both are about the DEVICE, never about a test:
# gradle's own sentence, and an emulator that is no longer attached now that it is over.
if [ $rc -ne 0 ]; then
  if grep -q "No connected devices" /tmp/pc-instrumented.log 2>/dev/null; then
    skip "the emulator was gone before any test executed (gradle: \"No connected devices!\")."
  fi
  # PLAY SERVICES TAKING US DOWN WITH IT IS NOT OUR TEST FAILING.
  #
  # `AppViewsLaunchSmokeTest` went red on two of five runs with "Instrumentation run failed due to
  # Process crashed" and, until the unfiltered logcat above existed, no cause anywhere. It is not a
  # crash. Run 34411867870, verbatim:
  #
  #   Killing 4939:place.poster.app (adj 0): depends on provider
  #   com.google.android.gms/.fonts.provider.FontsProvider
  #   in dying proc com.google.android.gms.persistent (adj -10000)
  #
  # The ActivityManager killed the app because GMS died under it — the google_apis image restarting
  # its own persistent process — and anything holding a provider in that process goes with it. The
  # app under test did nothing. Reported as a test failure it is worse than noise: it is a red build
  # that sends somebody looking for a bug in whatever view happened to be open, which is where two
  # rounds of reading already went.
  #
  # So it is exit 2, the same verdict this file already gives a missing emulator: nothing was
  # verified. NOT exit 0 — a run that could not ask is never a pass, and the job's own summary says
  # so. Matched against the LOGCAT rather than the gradle output, because gradle never sees it.
  if grep -qE "Killing [0-9]+:place\.poster\.app.*dying proc com\.google\.android\.gms" \
       /tmp/pc-androidtest/logcat-instrumented-full.txt 2>/dev/null; then
    skip "Play Services died and the ActivityManager killed the app under test with it (it depends on GMS's FontsProvider). Nothing here is a verdict on the code — see logcat-instrumented-full.txt."
  fi
  device_present || skip "the emulator disappeared partway through the run; the results are not a verdict on the code."
fi
exit $rc
