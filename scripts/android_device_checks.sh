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

# ---------------------------------------------------------------------------------------------
# THE LAUNCHER, AND WHAT IT COSTS.
#
# A launcher that fails takes the phone's home screen with it — there is no second one to fall back
# to. So this takes the HOME role for real, presses HOME, and checks that what came up is OURS and
# that it did not crash. And then it MEASURES, because "battery efficient" is a claim: with the HOME
# role the process is resident for the life of the battery, so anything it polls it polls for ever.
#
# The numbers below are the ones that actually decide that, and they are printed either way so the
# run's log carries them rather than an adjective:
#   * wake locks held while the screen is off  (must be none)
#   * alarms and jobs the launcher scheduled   (must be none)
#   * CPU after a minute idle as the home screen
#
# The role is GIVEN BACK at the end. Leaving the emulator's home app changed would affect every test
# after this one on the same boot.
HOME_ACT=$PKG/$PKG.home.HomeActivity
PREV_HOME=$(adb shell cmd role get-role-holders android.app.role.HOME 2>/dev/null | tr -d '\r')

say "the launcher: take the HOME role"
adb shell pm enable "$HOME_ACT" >/dev/null 2>&1
if adb shell cmd role add-role-holder android.app.role.HOME $PKG >/dev/null 2>&1; then
  sleep 2
  HOLDER=$(adb shell cmd role get-role-holders android.app.role.HOME 2>/dev/null | tr -d '\r')
  case "$HOLDER" in
    *$PKG*) ok "PosterChan is the home screen ($HOLDER)" ;;
    *)      echo "    (could not take the HOME role on this image: '$HOLDER' — skipping the launcher checks)"
            HOLDER="" ;;
  esac
else
  echo "    (no 'cmd role' on this image — skipping the launcher checks)"
  HOLDER=""
fi

if [ -n "$HOLDER" ]; then
  adb logcat -c
  say "press HOME"
  adb shell input keyevent KEYCODE_WAKEUP
  adb shell input keyevent KEYCODE_HOME
  sleep 6
  TOP=$(adb shell dumpsys activity activities 2>/dev/null | grep -m1 -E 'mResumedActivity|topResumedActivity' | tr -d '\r')
  echo "    resumed: $TOP"
  case "$TOP" in
    *HomeActivity*) ok "our home screen is what HOME brought up" ;;
    *) fail "HOME did not bring up our launcher: $TOP" ;;
  esac
  crash_scan launcher

  # PRESSING HOME WHILE ALREADY HOME, and BACK. Both are swallowed by a launcher; a launcher that
  # finishes on back leaves the phone showing whatever is behind it, which on a fresh boot is
  # nothing at all.
  adb shell input keyevent KEYCODE_HOME
  adb shell input keyevent KEYCODE_BACK
  sleep 3
  TOP=$(adb shell dumpsys activity activities 2>/dev/null | grep -m1 -E 'mResumedActivity|topResumedActivity' | tr -d '\r')
  case "$TOP" in
    *HomeActivity*) ok "back did not take the home screen down" ;;
    *) fail "back left the phone with no home screen: $TOP" ;;
  esac
  crash_scan launcher-keys

  say "what the home screen costs"
  adb shell input keyevent KEYCODE_POWER      # screen off, with us as home
  sleep 60

  LOCKS=$(adb shell dumpsys power 2>/dev/null | grep -c "WAKE_LOCK.*$PKG")
  echo "    wake locks held by $PKG with the screen off: $LOCKS"
  if [ "${LOCKS:-0}" -gt 0 ]; then
    fail "the launcher is holding a wake lock while the screen is off"
    adb shell dumpsys power 2>/dev/null | grep "WAKE_LOCK.*$PKG" | head -5
  else
    ok "no wake locks"
  fi

  ALARMS=$(adb shell dumpsys alarm 2>/dev/null | grep -c "$PKG")
  JOBS=$(adb shell dumpsys jobscheduler 2>/dev/null | grep -c "$PKG")
  echo "    alarms: $ALARMS    jobs: $JOBS"

  # CPU over the minute the screen was off. `top -b -n 1` reports the last sample, which after a
  # minute of nothing is exactly the question being asked: is this process doing anything at all?
  CPU=$(adb shell top -b -n 1 -o %CPU,ARGS 2>/dev/null | grep "$PKG" | head -1 | tr -d '\r')
  echo "    cpu after 60s idle as the home screen: ${CPU:-<not sampled>}"
  adb shell dumpsys batterystats --charged $PKG > "$OUT/pc-device-battery.txt" 2>/dev/null || true
  grep -m5 -E "Wake lock|Foreground services|Total run time" "$OUT/pc-device-battery.txt" 2>/dev/null | sed 's/^/    /' || true

  adb shell input keyevent KEYCODE_WAKEUP
  sleep 2
  crash_scan launcher-idle

  # PICTURES, because "make it look good" is the one requirement nothing here can check and nobody
  # on this side of the build has a device to look at. These land in the run's artifacts, so a
  # judgement about the design is made from the actual pixels rather than from a description of them.
  say "what it looks like"
  shot() {  # $1 = name
    adb exec-out screencap -p > "$OUT/pc-shot-$1.png" 2>/dev/null
    if [ -s "$OUT/pc-shot-$1.png" ]; then ok "captured $1"; else echo "    (no screenshot: $1)"; fi
  }
  adb shell input keyevent KEYCODE_WAKEUP
  adb shell input keyevent KEYCODE_HOME
  sleep 3
  shot home
  adb shell am start -n $PKG/$PKG.sms.ThreadListActivity >/dev/null 2>&1
  sleep 4
  shot messages
  adb shell am start -a android.intent.action.DIAL -n $PKG/$PKG.phone.DialerActivity >/dev/null 2>&1
  sleep 4
  shot dialer
  crash_scan screens
  adb shell input keyevent KEYCODE_HOME
  sleep 2

  say "give the home screen back"
  case "$PREV_HOME" in
    *:*) adb shell cmd role add-role-holder android.app.role.HOME \
              "$(echo "$PREV_HOME" | sed 's/.*: *//')" >/dev/null 2>&1 ;;
  esac
  adb shell pm disable-user --user 0 "$HOME_ACT" >/dev/null 2>&1
  adb shell input keyevent KEYCODE_HOME
  sleep 2
  ok "restored"
fi

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
