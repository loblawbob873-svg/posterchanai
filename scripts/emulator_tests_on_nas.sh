#!/usr/bin/env bash
# Run instrumented (androidTest) classes on a REAL emulator on nas.lan, from server1.
#
# WHY NAS. server1 has no /dev/kvm (its firmware has SVM off), and its Gradle cannot run either: the
# host's nft `ip nat postrouting` chain is an unconditional `masquerade`, which rewrites even LOOPBACK
# connections to come from 192.168.0.2 -- so the Gradle client's connection to its own daemon on
# 127.0.0.1 arrives "from a remote address" and the daemon refuses it ("The first result from the
# daemon was empty"). Measured with a 3-line python listen/connect: peer = ('192.168.0.2', ...).
# nas.lan has KVM and a sane loopback, so it builds AND boots.
#
# NAS ALSO SERVES PRODUCTION (media, torrents, office, Monero wallets). Everything here runs at
# nice 19 / ionice idle, the emulator gets 1536 MB like CI's, the Gradle daemon is stopped after the
# build, and the emulator is ALWAYS killed (trap on EXIT/INT/TERM/HUP, plus a hard `timeout` as the
# backstop if the trap itself never gets to run).
#
# Everything on nas lives under ~/android-sdk-emu (jdk17/, sdk/, avd/, user/, gradle/, work/); it
# touches nothing else there. One-time setup is described in setup_help below.
#
# Usage (from the repo root on server1):
#   scripts/emulator_tests_on_nas.sh [-c CLASS[#method]]... [-n REPEAT] [--no-build] [--no-www]
#                                    [--gpu MODE] [--keep-going]
#   -c   instrumented class to run (fully qualified, or relative to place.poster.app); repeatable.
#        Default: every class (the same set CI's connectedDebugAndroidTest runs).
#   -n   run each class REPEAT times on the same boot (flake check). Default 1.
#   --no-build  reuse the APKs already built on nas.
#   --no-www    skip build-www.sh + cap sync (use when only Java changed).
#   --gpu       emulator -gpu mode. Default swangle (what CI boots; see GPU= below).
#   --no-console  do not hand tests the emulator console (what CI's gradle run looks like).
#   --src DIR   build from this copy of mobile/ instead of the repo's (a deliberately broken copy,
#               to prove a test fails on the bug it guards).
# Results land in /tmp/pc-nas-emu/<stamp>/ on server1: instrument-*.txt (raw am instrument -r
# output), logcat.txt, crash.txt, timing.txt, summary.txt. Exit 0 = every run passed, 1 = a test
# failed, 2 = could not run (no build, no boot) -- never a pass.
set -uo pipefail

# THE WHOLE SCRIPT IS ONE BRACE GROUP, parsed before any of it runs. Bash otherwise reads a script as
# it goes, so editing this file during a 5-minute run (which happened) made the running copy execute
# half of the new text -- it died on a syntax error AFTER the emulator was up and never collected
# the results. The remote half has its own trap; this keeps the local half whole.
{
NAS=${PC_EMU_HOST:-nas.lan}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CLASSES=()
REPEAT=1
BUILD=1
WWW=1
# swangle, like CI, and not swiftshader_indirect: MEASURED on this nas emulator, the WebView RENDERER
# process SIGSEGV'd (null deref in libmonochrome, after "tile memory limits exceeded") in 3 of 4 runs of
# MusicBackgroundDeviceTest's rotation case under swiftshader_indirect and in 0 of 3 under swangle --
# a red that is the emulator's GPU emulation, not the app. --gpu swiftshader_indirect still selects it.
GPU=swangle
SRC_MOBILE=
CONSOLE=1
while [ $# -gt 0 ]; do
  case "$1" in
    -c) CLASSES+=("$2"); shift 2;;
    -n) REPEAT=$2; shift 2;;
    --no-build) BUILD=0; shift;;
    --no-www) WWW=0; shift;;
    --gpu) GPU=$2; shift 2;;
    --src) SRC_MOBILE=$(cd "$2" && pwd); shift 2;;
    --no-console) CONSOLE=0; shift;;
    -h|--help) sed -n 2,35p "$0"; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
for i in "${!CLASSES[@]}"; do
  case "${CLASSES[$i]}" in place.poster.app.*) ;; *) CLASSES[$i]="place.poster.app.${CLASSES[$i]}";; esac
done

STAMP=$(date +%Y%m%d-%H%M%S)
OUT=/tmp/pc-nas-emu/$STAMP
mkdir -p "$OUT"
echo "results: $OUT"

setup_help() {
  cat >&2 <<'EOF'
nas is not set up. One-time, under ~/android-sdk-emu on nas.lan:
  jdk17/   a JDK 17 (rsync server1's /opt/openjdk-bin-17*)          -- Gradle 8.13 + AGP refuse nas's JDK 25
           and REPLACE lib/security/cacerts with a real file (Gentoo's is a relative symlink to
           /etc/ssl/certs/java/cacerts, which from ~/android-sdk-emu resolves to nothing: every
           HTTPS fetch then dies with "trustAnchors parameter must be non-empty")
  sdk/     rsync server1's ~/android-sdk (platforms, build-tools, platform-tools, cmdline-tools), then
           emulator:  https://dl.google.com/android/repository/emulator-linux_x64-15917651.zip -> sdk/emulator
                      (the build CI pins, 37.1.11.0)
           image:     https://dl.google.com/android/repository/sys-img/google_apis/x86_64-34_r14.zip
                      -> sdk/system-images/android-34/google_apis/x86_64  (CI: api 34, google_apis, x86_64)
  avd/pc34.ini + avd/pc34.avd/config.ini   (hand-written; avdmanager wants an "emulator" package.xml)
           SAME SCREEN AS CI: hw.lcd.width=320 hw.lcd.height=640 hw.lcd.density=160 (avdmanager's default
           profile, which is what the CI action creates; read off a CI logcat, mBounds 320x640 @ 160dpi).
           A bigger (360x800) screen HID the dialer keypad jumping under the finger and skipped
           WidgetDeviceTest's ceiling case, which CI runs.
EOF
}

# Each remote step is SHIPPED AS A FILE and then run with stdin closed. Fed through `ssh bash -s`, the
# first child that reads stdin (the emulator, adb) swallowed the rest of the script: the first run here
# booted the emulator and then "finished" with exit 0 having run no test at all.
remote() {  # remote NAME [VAR=value ...] < script
  local name=$1; shift
  ssh "$NAS" "mkdir -p ~/android-sdk-emu/bin && cat > ~/android-sdk-emu/bin/$name" || return 2
  # shellcheck disable=SC2029
  ssh "$NAS" "$* bash ~/android-sdk-emu/bin/$name" < /dev/null
}

# ---- 0. ONE RUN AT A TIME on nas (2026-10-23) -------------------------------------------------------
# The work dir, the AVD, the adb port and the emulator are shared by every caller. Two sessions ran this at
# once: the second rsynced over the first's sources, installed its APK into the first's emulator (BOOT 0 --
# it found one up) and its EXIT trap killed it; both runs' results were garbage. mkdir is atomic on nas; the
# owner line says who holds it, and a lock older than 75 minutes (beyond the 45-minute hard timeout) is
# treated as left behind by a run that was SIGKILLed.
LOCK=android-sdk-emu/run.lock
lock_owner() { ssh "$NAS" "cat ~/$LOCK/owner 2>/dev/null"; }
take_lock() {
  # shellcheck disable=SC2029
  ssh "$NAS" "mkdir ~/$LOCK 2>/dev/null && echo '$(hostname) pid=$$ dir=$ROOT since=$(date +%s)' > ~/$LOCK/owner"
}
WAITED=0
until take_lock; do
  own=$(lock_owner); since=$(sed -n 's/.*since=\([0-9]*\).*/\1/p' <<<"$own")
  if [ -n "$since" ] && [ $(( $(date +%s) - since )) -gt 4500 ]; then
    echo "emulator lock is stale ($own) -- taking it" >&2; ssh "$NAS" "rm -rf ~/$LOCK"; continue
  fi
  if [ "$WAITED" -ge "${PC_EMU_LOCK_WAIT:-1800}" ]; then echo "emulator busy: $own -- gave up after ${WAITED}s" >&2; exit 2; fi
  [ "$WAITED" = 0 ] && echo "emulator busy: $own -- waiting (PC_EMU_LOCK_WAIT=${PC_EMU_LOCK_WAIT:-1800}s)" >&2
  sleep 15; WAITED=$((WAITED + 15))
done
trap 'ssh "$NAS" "rm -rf ~/$LOCK" 2>/dev/null' EXIT

# ---- 1. the web bundle (local: node lives on server1, not nas) ---------------------------------
if [ "$BUILD" = 1 ] && [ "$WWW" = 1 ]; then
  ( cd "$ROOT/mobile" \
    && { [ -d node_modules ] || npm ci --no-audit --no-fund; } \
    && bash build-www.sh >/dev/null \
    && npx cap sync android >/dev/null ) || { echo "web bundle / cap sync failed" >&2; exit 2; }
fi

# ---- 2. ship the sources and build on nas -------------------------------------------------------
T0=$(date +%s)
if [ "$BUILD" = 1 ]; then
  ssh "$NAS" 'test -x ~/android-sdk-emu/jdk17/bin/java && test -x ~/android-sdk-emu/sdk/emulator/emulator && test -f ~/android-sdk-emu/avd/pc34.ini' \
    || { setup_help; exit 2; }
  ssh "$NAS" 'mkdir -p ~/android-sdk-emu/work'
  rsync -a --delete \
    --exclude '/android/app/build/' --exclude '/android/build/' --exclude '/android/.gradle/' \
    --exclude '/android/*/build/' --exclude '/android/local.properties' \
    "${SRC_MOBILE:-$ROOT/mobile}/" "$NAS:android-sdk-emu/work/mobile/" || { echo "rsync failed" >&2; exit 2; }
  # shellcheck disable=SC2016
  remote build.sh > "$OUT/gradle.txt" 2>&1 <<'REMOTE'
set -uo pipefail
E=$HOME/android-sdk-emu
# HOME too: the emulator, adb and Gradle all write dotfiles (~/.android, the console auth token, adb
# keys); every one of them stays under ~/android-sdk-emu.
export HOME=$E/home
mkdir -p "$HOME"
export JAVA_HOME=$E/jdk17 ANDROID_HOME=$E/sdk ANDROID_SDK_ROOT=$E/sdk ANDROID_USER_HOME=$E/user GRADLE_USER_HOME=$E/gradle
# AND the JVM's user.home: Java reads it from the passwd entry, not $HOME, and the Android Gradle plugin
# wrote ~/.android/analytics.settings into the real home through it (measured on the first builds).
export JAVA_TOOL_OPTIONS="-Duser.home=$HOME"
mkdir -p "$ANDROID_USER_HOME"
cd "$E/work/mobile/android" || exit 2
echo "sdk.dir=$ANDROID_HOME" > local.properties
nice -n 19 ionice -c3 ./gradlew :app:assembleDebug :app:assembleDebugAndroidTest --console=plain
rc=$?
./gradlew --stop >/dev/null 2>&1 || true   # never leave a 1.5 GB daemon resident on a serving node
exit $rc
REMOTE
  rc=$?
  if [ $rc -ne 0 ]; then
    grep -E "error:|FAILED|What went wrong" -A3 "$OUT/gradle.txt" | head -40 >&2
    echo "build failed on $NAS (full log: $OUT/gradle.txt)" >&2
    exit 2
  fi
fi
T1=$(date +%s)
echo "build: $((T1 - T0))s" | tee "$OUT/timing.txt"

# ---- 3. boot, install, run, collect, and ALWAYS shut down ---------------------------------------
CLS="${CLASSES[*]:-}"
remote run.sh "CLS='$CLS' REPEAT='$REPEAT' GPU='$GPU' CONSOLE='$CONSOLE'" > "$OUT/run.txt" 2>&1 <<'REMOTE'
set -uo pipefail
E=$HOME/android-sdk-emu
export HOME=$E/home
mkdir -p "$HOME"
export ANDROID_HOME=$E/sdk ANDROID_SDK_ROOT=$E/sdk ANDROID_USER_HOME=$E/user ANDROID_AVD_HOME=$E/avd
# A private adb server port: never collide with (or kill) an adb anybody else on nas is using.
export ANDROID_ADB_SERVER_PORT=5137
PORT=5580
SERIAL=emulator-$PORT
ADB="$ANDROID_HOME/platform-tools/adb -s $SERIAL"
R=$E/results
rm -rf "$R"; mkdir -p "$R"
EMU_PID=
cleanup() {
  trap - EXIT INT TERM HUP
  if [ -n "$EMU_PID" ] && kill -0 "$EMU_PID" 2>/dev/null; then
    $ADB emu kill >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do kill -0 "$EMU_PID" 2>/dev/null || break; sleep 0.5; done
    kill -TERM "$EMU_PID" 2>/dev/null; sleep 2; kill -KILL "$EMU_PID" 2>/dev/null
    # The qemu child can outlive the launcher it was started by.
    pkill -KILL -f "qemu-system-x86_64.*-avd pc34" 2>/dev/null || true
  fi
  "$ANDROID_HOME/platform-tools/adb" kill-server >/dev/null 2>&1 || true
  echo "EMULATOR_STOPPED"
}
trap cleanup EXIT INT TERM HUP

APK=$(find "$E/work/mobile/android/app/build/outputs/apk/debug" -name '*.apk' | head -1)
TAPK=$(find "$E/work/mobile/android/app/build/outputs/apk/androidTest" -name '*.apk' | head -1)
[ -n "$APK" ] && [ -n "$TAPK" ] || { echo "NO_APKS"; exit 2; }

B0=$(date +%s)
# Hard backstop: even if this shell is SIGKILLed and the trap never runs, the emulator dies within
# 45 minutes. -no-snapshot: every run is a coherent cold boot, like CI's verdict boots.
nice -n 19 ionice -c3 timeout --kill-after=30s 45m "$ANDROID_HOME/emulator/emulator" -avd pc34 \
  -port $PORT -no-window -no-audio -no-boot-anim -no-snapshot -wipe-data -no-snapshot-save \
  -gpu "$GPU" -camera-back none -memory 1536 -cores 4 > "$R/emulator-console.txt" 2>&1 &
EMU_PID=$!
"$ANDROID_HOME/platform-tools/adb" start-server >/dev/null 2>&1
booted=
for _ in $(seq 1 300); do
  kill -0 "$EMU_PID" 2>/dev/null || { echo "EMULATOR_DIED"; tail -20 "$R/emulator-console.txt"; exit 2; }
  if [ "$($ADB shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; then booted=1; break; fi
  sleep 2
done
[ -n "$booted" ] || { echo "BOOT_TIMEOUT"; exit 2; }
B1=$(date +%s)
echo "BOOT_SECONDS $((B1 - B0))"
# What CI's emulator action does before its script: animations off, screen awake and unlocked.
for k in window_animation_scale transition_animation_scale animator_duration_scale; do
  $ADB shell settings put global $k 0 >/dev/null 2>&1
done
$ADB shell svc power stayon true >/dev/null 2>&1
$ADB shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
$ADB shell wm dismiss-keyguard >/dev/null 2>&1
# -g: the same granted-runtime-permission fixture scripts/android_instrumented.sh installs with.
$ADB install -r -g "$APK" > "$R/install.txt" 2>&1 || { echo "INSTALL_FAILED"; cat "$R/install.txt"; exit 2; }
$ADB install -r -g "$TAPK" >> "$R/install.txt" 2>&1 || { echo "INSTALL_FAILED"; cat "$R/install.txt"; exit 2; }
$ADB logcat -c >/dev/null 2>&1 || true

RUNNER=place.poster.app.test/androidx.test.runner.AndroidJUnitRunner
overall=0
run_one() {  # $1 = class (or empty = all), $2 = label
  local args=() t0 t1 f
  [ -n "$1" ] && args=(-e class "$1")
  f="$R/instrument-$2.txt"
  t0=$(date +%s)
  # 25 minutes like CI's instrumented step; the runner's own timeout_msec is 90s per test.
  # The console port + token let a test drive the emulator's REAL telephony from inside the guest
  # (10.0.2.2:<port>, e.g. `sms send`): SmsNotificationTapDeviceTest. CI's gradle run has neither and
  # those tests fall back to the app's own receiver. Exported by the emulator into $HOME at boot.
  local tok; tok=$(cat "$HOME/.emulator_console_auth_token" 2>/dev/null | tr -cd 'A-Za-z0-9+/=')
  local con=(-e pcEmuConsolePort "$PORT" -e pcEmuConsoleToken "${tok:-none}")
  [ "$CONSOLE" = 1 ] || con=()
  timeout --kill-after=10s 25m $ADB shell am instrument -w -r -e timeout_msec 90000 \
    "${con[@]}" "${args[@]}" "$RUNNER" > "$f" 2>&1
  t1=$(date +%s)
  # Verdict from the runner's own status codes: OK only with "OK (n tests)" and no failure codes.
  if grep -q '^OK (' "$f" && ! grep -qE 'INSTRUMENTATION_STATUS_CODE: -[1-9]|INSTRUMENTATION_FAILED|Process crashed' "$f"; then
    echo "RESULT PASS $2 $((t1 - t0))s $(grep '^OK (' "$f")"
  else
    overall=1
    echo "RESULT FAIL $2 $((t1 - t0))s $(grep -E '^(Tests run|FAILURES|OK \()' "$f" | tr '\n' ' ')"
  fi
}
if [ -z "$CLS" ]; then
  for n in $(seq 1 "$REPEAT"); do run_one "" "all-$n"; done
else
  for c in $CLS; do
    for n in $(seq 1 "$REPEAT"); do run_one "$c" "${c##*.}-$n"; done
  done
fi
echo "RUN_SECONDS $(( $(date +%s) - B1 ))"
$ADB logcat -d -v threadtime > "$R/logcat.txt" 2>/dev/null || true
$ADB logcat -d -b crash -v threadtime > "$R/crash.txt" 2>/dev/null || true
$ADB logcat -d -b radio -v threadtime > "$R/radio.txt" 2>/dev/null || true
exit $overall
REMOTE
rc=$?
rsync -a "$NAS:android-sdk-emu/results/" "$OUT/" 2>/dev/null || true
grep -E '^(BOOT_SECONDS|RUN_SECONDS)' "$OUT/run.txt" | tee -a "$OUT/timing.txt"
grep -E '^(RESULT|EMULATOR_|BOOT_TIMEOUT|NO_APKS|INSTALL_FAILED)' "$OUT/run.txt" | tee "$OUT/summary.txt"
grep -q EMULATOR_STOPPED "$OUT/run.txt" || echo "WARNING: no EMULATOR_STOPPED marker -- check nas for a stray qemu" >&2
# A run that printed no verdict line ran nothing, whatever its exit status says.
grep -q '^RESULT ' "$OUT/summary.txt" || { echo "no test verdicts were produced -- NOT a pass" >&2; exit 2; }
if [ "$rc" = 0 ] && grep -q '^RESULT FAIL' "$OUT/summary.txt"; then rc=1; fi
case $rc in 0|1|2) exit $rc;; *) exit 2;; esac
}
