#!/usr/bin/env bash
# Drive the real app on a real Android system and fail on what a static check cannot see.
#
# WHY THIS EXISTS. Every native guard in this repo matches TEXT in a Java source, because there was no
# device anywhere in the loop. Three background-sync bugs shipped in one day through a completely
# green suite — the strings the tests looked for were all present and the logic around them was
# wrong. Each round cost an APK build and somebody's evening. This is the loop that closes.
#
# WHAT IT CAN DECIDE THAT NOTHING ELSE HERE CAN:
#   * a CRASH, with the stack trace, instead of "the app keeps crashing" and a guess;
#   * DOZE — `dumpsys deviceidle force-idle` is the real thing, and it is the exact state background
#     sync exists for. A bug that reads a dozing device as offline is invisible everywhere else;
#   * the LIFECYCLE — screen off, backgrounded, alarms delivered by the real AlarmManager.
#
# It is deliberately NOISY on failure (logcat around the fault is printed, not just the assertion)
# and SILENT on success, so a red run tells you what happened without a second round trip.
set -uo pipefail

PKG=place.poster.app
OUT=/tmp
FAILED=0

say()  { printf '\n=== %s\n' "$*"; }
fail() { printf '\nFAIL: %s\n' "$*"; FAILED=1; }
ok()   { printf 'ok: %s\n' "$*"; }

APK=$(find mobile/android -path '*debug*' -name '*.apk' | head -1)
[ -n "$APK" ] || { echo "no debug APK built"; exit 1; }

say "install $APK"
adb install -r -g "$APK" || { echo "install failed"; exit 1; }

# -g grants runtime permissions, but the SAF tree grant and the account are user gestures we cannot
# make here. So these checks assert what is reachable without them: that the app STARTS, that its
# background machinery is wired, and that nothing throws. A sweep of real files needs a real grant
# and is out of scope — saying so is better than a check that pretends.

adb logcat -c

say "launch"
adb shell monkey -p $PKG -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
sleep 20

crash_scan() {   # $1 = label
  adb logcat -d > "$OUT/pc-logcat-$1.txt" 2>/dev/null
  # A FATAL EXCEPTION from OUR package is a failure. Other apps' noise on the emulator is not ours.
  if grep -q "FATAL EXCEPTION" "$OUT/pc-logcat-$1.txt" && grep -q "$PKG" "$OUT/pc-logcat-$1.txt"; then
    fail "the app crashed during: $1"
    echo "---- the trace ----"
    grep -A 40 "FATAL EXCEPTION" "$OUT/pc-logcat-$1.txt" | head -60
    echo "-------------------"
    return 1
  fi
  # ANRs and the foreground-service timeout are crashes by another name.
  for pat in "ANR in $PKG" "ForegroundServiceDidNotStartInTime" "ForegroundServiceStartNotAllowed" \
             "RemoteServiceException"; do
    if grep -q "$pat" "$OUT/pc-logcat-$1.txt"; then
      fail "$pat during: $1"
      grep -B 2 -A 20 "$pat" "$OUT/pc-logcat-$1.txt" | head -40
      return 1
    fi
  done
  ok "no crash during: $1"
}

crash_scan launch

say "the process is actually up"
if adb shell pidof $PKG >/dev/null 2>&1; then ok "running"; else fail "the app is not running after launch"; fi

say "background it — screen off, which is where every report starts"
adb shell input keyevent KEYCODE_HOME
sleep 3
adb shell input keyevent KEYCODE_POWER      # screen off
sleep 12
crash_scan screen-off

say "what the app scheduled"
adb shell dumpsys alarm > "$OUT/pc-device-alarms.txt" 2>/dev/null
if grep -q "$PKG" "$OUT/pc-device-alarms.txt"; then
  ok "the app holds alarms with the real AlarmManager"
  grep -c "$PKG" "$OUT/pc-device-alarms.txt" | sed 's/^/    entries: /'
else
  # Not fatal: with no folder paired there is nothing to arm, and pairing needs a user gesture.
  echo "    (no alarms — expected with no folder paired on a fresh install)"
fi
adb shell dumpsys jobscheduler > "$OUT/pc-device-jobs.txt" 2>/dev/null || true

say "real Doze"
adb shell dumpsys battery unplug            >/dev/null 2>&1
adb shell dumpsys deviceidle enable         >/dev/null 2>&1
adb shell dumpsys deviceidle force-idle     >/dev/null 2>&1
sleep 10
adb shell dumpsys deviceidle | head -5 | sed 's/^/    /'
crash_scan doze

say "and out of Doze again"
adb shell dumpsys deviceidle unforce        >/dev/null 2>&1
adb shell dumpsys battery reset             >/dev/null 2>&1
adb shell input keyevent KEYCODE_WAKEUP
sleep 3
adb shell monkey -p $PKG -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
sleep 15
crash_scan resume

say "the app survived the whole cycle"
if adb shell pidof $PKG >/dev/null 2>&1; then ok "still running"; else fail "the app died during the cycle"; fi

adb logcat -d > "$OUT/pc-logcat-full.txt" 2>/dev/null

if [ "$FAILED" -ne 0 ]; then
  echo
  echo "One or more device checks failed. Full logcat is in the run's artifacts."
  exit 1
fi
echo
echo "All device checks passed."
