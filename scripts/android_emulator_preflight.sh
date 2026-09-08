#!/usr/bin/env bash
# Probe the same headless executable used by the verdict boots. A plain -version selects
# Qt QEMU and requires desktop libpulse even when the actual no-window emulator is usable.
set -euo pipefail
touch "${PC_EMULATOR_CONSOLE:-/tmp/pc-emulator-console.txt}"
cmp -s "$ANDROID_HOME/emulator/emulator" scripts/android_emulator_supervisor.sh
timeout --kill-after=2s 10s "$ANDROID_HOME/emulator/emulator" -no-window -noaudio -version | grep -F '37.1.11.0 (build_id 15917651)'
