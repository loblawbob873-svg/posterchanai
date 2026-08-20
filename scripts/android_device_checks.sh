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
# TWO WAYS IN, because `cmd role` is not on every image and the first version SKIPPED THE WHOLE
# LAUNCHER CHECK when it was missing — which is what happened on every run: "(no 'cmd role' on this
# image)" and then nothing about the launcher was ever exercised. `cmd package set-home-activity` is
# the older, more widely present route and is what actually works on the API-34 google_apis image.
adb shell cmd package set-home-activity "$HOME_ACT" >/dev/null 2>&1
adb shell cmd role add-role-holder android.app.role.HOME $PKG >/dev/null 2>&1
sleep 2
# THE AUTHORITY IS WHAT COMES UP WHEN YOU PRESS HOME — not what a command printed. `set-home-activity`
# answers "Success" on this image and `resolve-activity` still names the stock launcher, because with
# two home apps installed the query has no single answer. Pressing the key does.
#
# If ours is not what appears, the stock launcher is disabled so that it is: the AVD is ephemeral and
# both are put back at the end. Skipping instead is what left the launcher unexercised on every run.
# WAKE, THEN UNLOCK, and the second one is not optional. The section above deliberately turns the
# screen off, so the device comes back on the KEYGUARD — and a HOME press against a locked device
# resolves to `com.android.settings/.FallbackHome`, the placeholder Android shows a user who has not
# unlocked yet. That is not our launcher and not the stock one, so the check then stood the stock
# launcher down (which changed nothing, because the stock launcher was never what was on top),
# pressed HOME again, saw FallbackHome again and failed with "HOME did not bring up our launcher".
# Every emulator run reported the launcher as broken for a reason that was entirely about the lock
# screen. `wm dismiss-keyguard` is the direct form; MENU is the fallback for images without it.
adb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
adb shell wm dismiss-keyguard >/dev/null 2>&1
adb shell input keyevent KEYCODE_MENU >/dev/null 2>&1
sleep 1
adb shell input keyevent KEYCODE_HOME
sleep 3
TOP=$(adb shell dumpsys activity activities 2>/dev/null | grep -m1 -E 'mResumedActivity|topResumedActivity' | tr -d '\r')
echo "    after HOME: $TOP"
STOCK=""
case "$TOP" in
  *HomeActivity*) ok "PosterChan is the home screen" ; HOLDER="ours" ;;
  *)
    STOCK=$(adb shell "cmd package query-activities -a android.intent.action.MAIN -c android.intent.category.HOME 2>/dev/null" \
            | grep -o '[a-z0-9._]*launcher[a-z0-9._]*' | grep -v "$PKG" | sort -u | head -1 | tr -d '\r')
    if [ -n "$STOCK" ]; then
      echo "    standing the stock launcher down for the test: $STOCK"
      adb shell pm disable-user --user 0 "$STOCK" >/dev/null 2>&1
      adb shell input keyevent KEYCODE_HOME
      sleep 3
      TOP=$(adb shell dumpsys activity activities 2>/dev/null | grep -m1 -E 'mResumedActivity|topResumedActivity' | tr -d '\r')
      echo "    after HOME: $TOP"
    fi
    case "$TOP" in
      *HomeActivity*) ok "PosterChan is the home screen" ; HOLDER="ours" ;;
      *) fail "HOME did not bring up our launcher — it was NOT exercised: $TOP" ; HOLDER="" ;;
    esac
    ;;
esac

if [ -n "$HOLDER" ]; then
  adb logcat -c
  say "press HOME"
  adb shell input keyevent KEYCODE_WAKEUP
  adb shell wm dismiss-keyguard >/dev/null 2>&1
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
  # THREE APPS IN THE DRAWER, asserted rather than eyeballed. Only MainActivity used to carry a
  # MAIN/LAUNCHER filter, so Messages and Phone could be ROUTED to as the phone's default handlers
  # and appeared in no launcher at all — ours or the stock one. "my point is that there is no phone
  # app/icon for it!" was exactly right: routing is not an app.
  say "the apps that appear in a launcher"
  LAUNCHABLE=$(adb shell "cmd package query-activities -a android.intent.action.MAIN -c android.intent.category.LAUNCHER 2>/dev/null | grep -c $PKG" | tr -d '\r')
  echo "    launcher entries for $PKG: ${LAUNCHABLE:-0}"
  adb shell "cmd package query-activities -a android.intent.action.MAIN -c android.intent.category.LAUNCHER 2>/dev/null" \
    | grep -o "$PKG/[A-Za-z0-9_.]*" | sort -u | sed 's/^/      /'
  # FOUR NOW: PosterChan, Messages, Phone and Email. "no Email app phone launcher either" — and
  # Email is the one that cannot be an alias onto a native activity, because the mail client is a
  # view inside the WebView; it is an alias over the .shortcut.ViewActivity trampoline.
  if [ "${LAUNCHABLE:-0}" -ge 4 ]; then
    ok "PosterChan, Messages, Phone and Email are all launchable"
  else
    fail "fewer than four launcher entries — Messages, Phone or Email has no icon in any drawer"
  fi

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

  # A KEY MID-PRESS. The glow is a state, so it only exists while a finger is down — a screenshot
  # taken after the tap shows the idle key and proves nothing. `input swipe` with a long duration and
  # no movement IS a held press; backgrounding it and capturing partway through is the only way to
  # photograph it.
  W=$(adb shell wm size | grep -o '[0-9]*x[0-9]*' | head -1 | cut -dx -f1 | tr -d '\r')
  H=$(adb shell wm size | grep -o '[0-9]*x[0-9]*' | head -1 | cut -dx -f2 | tr -d '\r')
  if [ -n "${W:-}" ] && [ -n "${H:-}" ]; then
    KX=$((W / 2)); KY=$((H * 62 / 100))
    (adb shell input swipe $KX $KY $KX $KY 1500 >/dev/null 2>&1 &)
    sleep 1
    shot dialer-key-pressed
    sleep 1
  fi
  crash_scan screens

  # THE DRAWER, MID-SWIPE. It opens by swiping up from the home surface now — the button is off the
  # dock — so this is both the picture and the only end-to-end proof the gesture works at all.
  adb shell input keyevent KEYCODE_HOME
  sleep 2
  if [ -n "${W:-}" ] && [ -n "${H:-}" ]; then
    (adb shell input swipe $((W / 2)) $((H * 80 / 100)) $((W / 2)) $((H * 25 / 100)) 500 >/dev/null 2>&1 &)
    sleep 0.4
    shot drawer-mid-swipe
    sleep 1.2
    shot drawer
    TOP=$(adb shell dumpsys activity activities 2>/dev/null | grep -m1 -E 'mResumedActivity|topResumedActivity' | tr -d '\r')
    case "$TOP" in
      *HomeActivity*) ok "the swipe stayed on the home screen (drawer is a layer, not an activity)" ;;
      *) echo "    resumed after swipe: $TOP" ;;
    esac
  fi
  crash_scan drawer
  adb shell input keyevent KEYCODE_HOME
  sleep 2

  # ---------------------------------------------------------------------------------------------
  # TABLET MODE, ON THE SAME EMULATOR. "the launcher needs to work on tablet mode too".
  #
  # `wm size` and `wm density` reshape the running device, which is the whole measurement: a
  # 2560x1600 screen at 240dpi has a short side of 1066dp, so Android reports it as a large screen
  # and HomeMetrics gives a wider grid, a longer dock and bigger icons. No second AVD, no second
  # boot — and it exercises exactly the path a real tablet takes, because a resize arrives as the
  # same configuration change a rotation does.
  #
  # It is put back unconditionally. `wm size reset` on a device left resized would otherwise poison
  # every check after this one, and the AVD is reused within the boot.
  say "tablet mode: a wider grid, a longer dock, and it must not crash"
  adb shell wm size 2560x1600 >/dev/null 2>&1
  adb shell wm density 240 >/dev/null 2>&1
  sleep 3
  adb shell input keyevent KEYCODE_HOME
  sleep 3
  TOP=$(adb shell dumpsys activity activities 2>/dev/null | grep -m1 -E 'mResumedActivity|topResumedActivity' | tr -d '\r')
  case "$TOP" in
    *HomeActivity*) ok "the launcher survived being resized to a tablet" ;;
    *) fail "the home screen did not come back after a tablet-sized configuration change: $TOP" ;;
  esac
  shot tablet-home
  # The drawer at that size too — its columns come from the GridView's column width, which is the
  # one part of the tablet layout that is not HomeMetrics arithmetic.
  TW=2560; TH=1600
  (adb shell input swipe $((TW / 2)) $((TH * 80 / 100)) $((TW / 2)) $((TH * 25 / 100)) 500 >/dev/null 2>&1 &)
  sleep 2
  shot tablet-drawer
  crash_scan tablet
  adb shell input keyevent KEYCODE_HOME
  adb shell wm size reset >/dev/null 2>&1
  adb shell wm density reset >/dev/null 2>&1
  sleep 3
  adb shell input keyevent KEYCODE_HOME
  sleep 2
  crash_scan tablet-reset

  say "give the home screen back"
  case "$PREV_HOME" in
    *:*) adb shell cmd role add-role-holder android.app.role.HOME \
              "$(echo "$PREV_HOME" | sed 's/.*: *//')" >/dev/null 2>&1 ;;
  esac
  adb shell pm disable-user --user 0 "$HOME_ACT" >/dev/null 2>&1
  [ -n "${STOCK:-}" ] && adb shell pm enable "$STOCK" >/dev/null 2>&1
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
