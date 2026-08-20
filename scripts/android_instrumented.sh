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
# --info so a failing assertion arrives with its message rather than as "tests failed".
./gradlew connectedDebugAndroidTest --console=plain
rc=$?
# The HTML/XML report is the only place per-test failures are legible; publish it either way.
mkdir -p /tmp/pc-androidtest
cp -r app/build/reports/androidTests/connected/. /tmp/pc-androidtest/ 2>/dev/null || true
cp -r app/build/outputs/androidTest-results/connected/. /tmp/pc-androidtest/ 2>/dev/null || true
exit $rc
