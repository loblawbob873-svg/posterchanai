#!/usr/bin/env bash
# Preserve the actual emulator child's exit status; the action launches it through `sh ... &`.
# Installed beside emulator.pc-real in the SDK; non-AVD probes retain original behavior.
real=${PC_EMULATOR_REAL:-$(dirname "$0")/emulator.pc-real}
case " $* " in *' -avd '*) ;; *) exec "$real" "$@";; esac
out=${PC_EMULATOR_EVIDENCE:-/tmp/pc-emulator-supervisor.txt}
# Do not hold the action launch shell's output pipe open for the emulator lifetime.
exec >> "$out" 2>&1
record() {
  {
    printf 'time=%s supervisor=%s child=%s %s\n' "$(date -u +%FT%TZ)" "$$" "${child:-none}" "$1"
    for file in /sys/fs/cgroup/memory.events /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max; do
      if [ -r "$file" ]; then printf '%s\n' "$file"; cat "$file"; fi
    done
  } >> "$out"
}
ulimit -c unlimited 2>/dev/null || true
"$real" "$@" &
child=$!
record started
# Forward service cleanup signals and reap the same child before recording its result.
trap 'kill -TERM "$child" 2>/dev/null || true' TERM HUP
trap 'kill -INT "$child" 2>/dev/null || true' INT
wait "$child"; result=$?
if kill -0 "$child" 2>/dev/null; then wait "$child"; result=$?; fi
signal=0
if [ "$result" -gt 128 ]; then signal=$((result-128)); fi
record "exit=$result signal=$signal"
exit "$result"
