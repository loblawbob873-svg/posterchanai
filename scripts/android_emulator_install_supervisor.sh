#!/usr/bin/env bash
# Put the exit-evidence supervisor in front of the real emulator -- AFTER the emulator action has
# installed its emulator, which is why this runs in the pre-launch hook and not as an earlier step.
# The earlier step was overwritten the day a newer emulator was released: the action's "install"
# replaced the wrapper, the preflight rightly refused, and every APK build stopped (2026-10-01).
# Idempotent: a wrapper already in place is left alone.
set -euo pipefail
emu="$ANDROID_HOME/emulator/emulator"
sup="scripts/android_emulator_supervisor.sh"
if ! cmp -s "$emu" "$sup"; then
    mv -f "$emu" "$emu.pc-real"
    cp "$sup" "$emu"
    chmod +x "$emu"
fi
