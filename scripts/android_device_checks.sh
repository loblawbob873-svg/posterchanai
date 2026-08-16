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
#
# EVERY adb CALL IS BOUNDED, AND THAT IS NOT DEFENSIVE PROGRAMMING — IT IS THE FIRST THING THIS
# SCRIPT GOT WRONG. On its first run ever the emulator booted in ten seconds, the APK installed, the
# log printed "=== launch", and then the job sat in silence until GitHub cancelled it fifty-five
# minutes later. The artifact said which line: a ZERO-BYTE pc-logcat-launch.txt — the redirect had
# opened the file, so `adb logcat -d` had started and never returned. The cause is in that same run's
# own boot log, `androidboot.logcat=*:V`: an AVD logs everything at verbose, so a full dump races a
# buffer being refilled faster than it drains. A hang has to become a FAILING CHECK IN SECONDS,
# because a job timeout names no step and costs a runner hour to learn nothing.
#
# The three rules that follow from it:
#   1. every adb call has a `timeout`, and a call that times out FAILS the check by name;
#   2. the log buffer is CLEARED at the start of each phase, so each dump is one phase's lines
#      rather than the whole boot, and is bounded again by `-b main,crash -t N`;
#   3. A DUMP THAT DID NOT COMPLETE IS NEVER READ AS "NO CRASH". An unreadable probe reporting
#      health is the exact failure the /logs board was rebuilt to stop making; it is a false green,
#      and a false green on a crash check is worse than having no check.
#
# $PC_ADB / $PC_OUT exist so tests/test_android_device_checks.py can RUN this logic against a stub
# adb — grep the wiring, run the logic.
set -uo pipefail

PKG=place.poster.app
ACT=$PKG/.MainActivity
ADB=${PC_ADB:-adb}
OUT=${PC_OUT:-/tmp}

# Generous enough that a slow emulator is not called a hang, short enough that a real hang is a
# red run in a minute rather than at the job timeout.
ADB_SECS=${PC_ADB_SECS:-60}
LOGCAT_SECS=${PC_LOGCAT_SECS:-90}
LOGCAT_LINES=${PC_LOGCAT_LINES:-4000}
SETTLE=${PC_SETTLE:-1}          # scaled down by the test; the real cycle needs real seconds

FAILED=0

say()  { printf '\n=== %s\n' "$*"; }
fail() { printf '\nFAIL: %s\n' "$*"; FAILED=1; }
ok()   { printf 'ok: %s\n' "$*"; }
nap()  { sleep $(( $1 * SETTLE )); }

# Bounded adb. $1 is the budget in seconds, the rest is the adb command line. Exit 124 is `timeout`
# saying it killed the call — the one outcome that must never be confused with a command that ran
# and answered nothing.
adbt() { local s=$1; shift; timeout "$s" "$ADB" "$@"; }

APK=${PC_APK:-$(find mobile/android -path '*debug*' -name '*.apk' 2>/dev/null | head -1)}
[ -n "$APK" ] || { echo "no debug APK built"; exit 1; }

say "install $APK"
adbt 300 install -r -g "$APK" || { echo "install failed (or timed out)"; exit 1; }

# -g grants runtime permissions, but the SAF tree grant and the account are user gestures we cannot
# make here. So these checks assert what is reachable without them: that the app STARTS, that its
# background machinery is wired, and that nothing throws. A sweep of real files needs a real grant
# and is out of scope — saying so is better than a check that pretends.

# Clear once per phase. Keeping each dump to one phase's lines is what makes the dump finish at all,
# and it is also what makes a printed trace readable instead of a boot log with a crash buried in it.
clear_log() { adbt "$ADB_SECS" logcat -c >/dev/null 2>&1 || true; }

# Fill $OUT/pc-logcat-$1.txt. Returns 1 when the log COULD NOT BE READ, which is a different answer
# from "read it, found nothing" and is never allowed to collapse into it.
capture() {
  local label=$1 f="$OUT/pc-logcat-$1.txt" rc
  : > "$f"
  timeout "$LOGCAT_SECS" "$ADB" logcat -d -b main,crash -t "$LOGCAT_LINES" > "$f" 2>/dev/null
  rc=$?
  if [ "$rc" -eq 124 ]; then
    fail "logcat did not finish within ${LOGCAT_SECS}s during: $label"
    echo "     The device is writing the buffer faster than it drains, or adb is wedged."
    echo "     THIS IS NOT A PASS: nothing was read, so nothing can be said about a crash."
    return 1
  fi
  if [ "$rc" -ne 0 ]; then
    fail "logcat could not be read (adb exit $rc) during: $label — no verdict on a crash"
    return 1
  fi
  return 0
}

crash_scan() {   # $1 = label
  local label=$1 f="$OUT/pc-logcat-$1.txt"
  capture "$label" || return 1
  # A FATAL EXCEPTION from OUR package is a failure. Other apps' noise on the emulator is not ours.
  if grep -q "FATAL EXCEPTION" "$f" && grep -q "$PKG" "$f"; then
    fail "the app crashed during: $label"
    echo "---- the trace ----"
    grep -A 40 "FATAL EXCEPTION" "$f" | head -60
    echo "-------------------"
    return 1
  fi
  # ANRs and the foreground-service timeout are crashes by another name.
  for pat in "ANR in $PKG" "ForegroundServiceDidNotStartInTime" "ForegroundServiceStartNotAllowed" \
             "RemoteServiceException"; do
    if grep -q "$pat" "$f"; then
      fail "$pat during: $label"
      grep -B 2 -A 20 "$pat" "$f" | head -40
      return 1
    fi
  done
  ok "no crash during: $label"
}

clear_log

say "launch"
# `am start -W`, NOT `monkey`. monkey is an event injector that happens to be able to open an app: it
# holds the adb shell open, and when anything it is waiting on does not arrive it waits for ever with
# its output discarded — indistinguishable, from out here, from an app that is simply slow. `am start
# -W` waits for the launch to complete and then EXITS, and prints the status it saw.
adbt "$ADB_SECS" shell am start -W -n "$ACT" 2>&1 | sed 's/^/    /'
nap 20

crash_scan launch

say "the process is actually up"
if adbt 30 shell pidof $PKG >/dev/null 2>&1; then ok "running"; else fail "the app is not running after launch"; fi

say "background it — screen off, which is where every report starts"
clear_log
adbt 30 shell input keyevent KEYCODE_HOME
nap 3
adbt 30 shell input keyevent KEYCODE_POWER      # screen off
nap 12
crash_scan screen-off

say "what the app scheduled"
adbt "$ADB_SECS" shell dumpsys alarm > "$OUT/pc-device-alarms.txt" 2>/dev/null
if grep -q "$PKG" "$OUT/pc-device-alarms.txt" 2>/dev/null; then
  ok "the app holds alarms with the real AlarmManager"
  grep -c "$PKG" "$OUT/pc-device-alarms.txt" | sed 's/^/    entries: /'
else
  # Not fatal: with no folder paired there is nothing to arm, and pairing needs a user gesture.
  echo "    (no alarms — expected with no folder paired on a fresh install)"
fi
adbt "$ADB_SECS" shell dumpsys jobscheduler > "$OUT/pc-device-jobs.txt" 2>/dev/null || true

say "real Doze"
clear_log
adbt 30 shell dumpsys battery unplug            >/dev/null 2>&1
adbt 30 shell dumpsys deviceidle enable         >/dev/null 2>&1
adbt 30 shell dumpsys deviceidle force-idle     >/dev/null 2>&1
nap 10
adbt 30 shell dumpsys deviceidle 2>/dev/null | head -5 | sed 's/^/    /'
crash_scan doze

say "and out of Doze again"
adbt 30 shell dumpsys deviceidle unforce        >/dev/null 2>&1
adbt 30 shell dumpsys battery reset             >/dev/null 2>&1
adbt 30 shell input keyevent KEYCODE_WAKEUP
nap 3
clear_log
adbt "$ADB_SECS" shell am start -W -n "$ACT" >/dev/null 2>&1
nap 15
crash_scan resume

say "the app survived the whole cycle"
if adbt 30 shell pidof $PKG >/dev/null 2>&1; then ok "still running"; else fail "the app died during the cycle"; fi

# THE APP'S OWN CRASH LOG, which is the surface that exists because logcat is unreadable in the
# field — no cable, no developer options, no adb. Reading it HERE too is what keeps the two honest:
# a crash that logcat missed still lands in this file, and a file that never appears while logcat
# shows a FATAL EXCEPTION means the handler is not installed on the path that died. `run-as` works
# because this is the debug APK.
say "the app's own crash log"
CRASH="$OUT/pc-device-crashlog.txt"
if adbt 30 shell run-as $PKG cat files/crash-log.txt > "$CRASH" 2>/dev/null && [ -s "$CRASH" ]; then
  fail "the app recorded a crash of its own"
  echo "---- crash-log.txt ----"
  head -60 "$CRASH"
  echo "-----------------------"
else
  ok "the app recorded no crash"
fi

adbt "$LOGCAT_SECS" logcat -d -b main,crash -t "$LOGCAT_LINES" > "$OUT/pc-logcat-full.txt" 2>/dev/null || true

if [ "$FAILED" -ne 0 ]; then
  echo
  echo "One or more device checks failed. Full logcat is in the run's artifacts."
  exit 1
fi
echo
echo "All device checks passed."
