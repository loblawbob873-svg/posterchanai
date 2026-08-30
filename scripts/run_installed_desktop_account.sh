#!/bin/sh
# Run the account-dependent installed Desktop gate in an invisible, isolated Sway compositor.
# This must never launch a second surface in the person's live desktop session.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
host=${1:?usage: run_installed_desktop_account.sh user@host [office|full|current|code]}
mode=${2:-office}
case "$mode" in office|full|current|code) ;; *) echo "mode must be office, full, current, or code" >&2; exit 2;; esac

token=installedacct12
root=/tmp/pc-installed-diagnostic.$token
runtime=$root/runtime
profile=$root/profile
key=$root/test.nsec
fixture=$root/files
code_root=/tmp/pc-code-installed.$token
port=${PC_CHECK_PORT:-9223}
tunnel_pid=

safe_root() {
  [ "$root" = "/tmp/pc-installed-diagnostic.installedacct12" ] || {
    echo "refusing cleanup outside the fixed diagnostic domain" >&2
    exit 3
  }
}

remote_cleanup() {
  safe_root
  ssh -o BatchMode=yes "$host" "
    root='$root'
    [ \"\$root\" = '/tmp/pc-installed-diagnostic.installedacct12' ] || exit 3
    if [ -r \"\$root/electron.pid\" ]; then kill \"\$(sed -n '1p' \"\$root/electron.pid\")\" 2>/dev/null || :; fi
    if [ -r \"\$root/sway.pid\" ]; then kill \"\$(sed -n '1p' \"\$root/sway.pid\")\" 2>/dev/null || :; fi
    find \"\$root\" -depth -mindepth 1 -delete 2>/dev/null || :
    rmdir \"\$root\" 2>/dev/null || :
    code_root='$code_root'
    [ \"\$code_root\" = '/tmp/pc-code-installed.installedacct12' ] || exit 3
    find \"\$code_root\" -depth -mindepth 1 -delete 2>/dev/null || :
    rmdir \"\$code_root\" 2>/dev/null || :
  " >/dev/null 2>&1 || :
}

cleanup() {
  [ -n "$tunnel_pid" ] && kill "$tunnel_pid" 2>/dev/null || :
  remote_cleanup
  safe_root
  find "$root" -depth -mindepth 1 -delete 2>/dev/null || :
  rmdir "$root" 2>/dev/null || :
}
trap cleanup EXIT HUP INT TERM

remote_cleanup
safe_root
mkdir -p "$root"
chmod 700 "$root"
mkdir -p "$fixture"
printf '%s\n' 'installed=true' >"$fixture/posterchan-installed.conf"
printf '%s\n' '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="#713dd8"/></svg>' >"$fixture/posterchan-installed.svg"
"$repo/.venv/bin/python" - "$key" <<'PY'
import os
import sys
from app.services.nostr import bech32

path = sys.argv[1]
old = os.umask(0o077)
try:
    with open(path, "w", encoding="ascii") as handle:
        handle.write(bech32.encode("nsec", os.urandom(32)))
finally:
    os.umask(old)
PY

ssh -o BatchMode=yes "$host" "
  root='$root'; runtime='$runtime'; profile='$profile'
  mkdir -p \"\$runtime\" \"\$profile\"
  chmod 700 \"\$root\" \"\$runtime\" \"\$profile\"
  if [ '$mode' = current ] || [ '$mode' = code ]; then
    source=\"\$HOME/.config/posterchan-desktop\"
    [ -d \"\$source\" ] || { echo 'installed Desktop profile is absent' >&2; exit 4; }
    cp -a \"\$source/.\" \"\$profile/\"
    find \"\$profile\" -maxdepth 1 -name 'Singleton*' -delete
  fi
  XDG_RUNTIME_DIR=\"\$runtime\" WLR_BACKENDS=headless WLR_HEADLESS_OUTPUTS=1 \
    nohup sway -c /dev/null >\"\$root/sway.log\" 2>&1 &
  echo \$! >\"\$root/sway.pid\"
"
scp -q -r "$fixture" "$host:$root/"
if [ "$mode" = code ]; then
  ssh -o BatchMode=yes "$host" "
    code_root='$code_root'
    [ \"\$code_root\" = '/tmp/pc-code-installed.installedacct12' ] || exit 3
    mkdir -p \"\$code_root\"
    git -C \"\$code_root\" init -q
    git -C \"\$code_root\" config user.name 'PosterChan verifier'
    git -C \"\$code_root\" config user.email 'verifier@invalid'
    printf '%s\n' 'const installedCode = false;' >\"\$code_root/changed.js\"
    git -C \"\$code_root\" add changed.js
    git -C \"\$code_root\" commit -qm fixture
    printf '%s\n' 'const installedCode = true;' >\"\$code_root/changed.js\"
  "
fi

socket=
i=0
while [ "$i" -lt 80 ]; do
  socket=$(ssh -o BatchMode=yes "$host" \
    "find '$runtime' -maxdepth 1 -type s -name 'sway-ipc.*.sock' -print -quit")
  [ -n "$socket" ] && break
  i=$((i + 1)); sleep .25
done
[ -n "$socket" ] || { echo "isolated Sway compositor did not start" >&2; exit 1; }

ssh -o BatchMode=yes "$host" "
  root='$root'; runtime='$runtime'; profile='$profile'
  XDG_RUNTIME_DIR=\"\$runtime\" WAYLAND_DISPLAY=wayland-1 SWAYSOCK='$socket' \
    PC_DIAGNOSTIC_TOKEN='$token' \
    nohup /opt/posterchan/posterchan-desktop --shell --ozone-platform=wayland \
      --remote-debugging-address=127.0.0.1 --remote-debugging-port='$port' \
      --pc-diagnostic-token='$token' --pc-diagnostic-profile=\"\$profile\" \
      --pc-diagnostic-swaysock='$socket' >\"\$root/electron.log\" 2>&1 &
  echo \$! >\"\$root/electron.pid\"
"

i=0
while [ "$i" -lt 120 ]; do
  if ssh -o BatchMode=yes "$host" "curl -fsS http://127.0.0.1:$port/json/version >/dev/null"; then
    break
  fi
  i=$((i + 1)); sleep .25
done
[ "$i" -lt 120 ] || {
  ssh -o BatchMode=yes "$host" "tail -80 '$root/electron.log'; tail -40 '$root/sway.log'" >&2
  exit 1
}

ssh -N -o ExitOnForwardFailure=yes -o BatchMode=yes \
  -L "$port:127.0.0.1:$port" "$host" &
tunnel_pid=$!
i=0
while [ "$i" -lt 40 ]; do
  if curl -fsS "http://127.0.0.1:$port/json/version" >/dev/null; then break; fi
  i=$((i + 1)); sleep .1
done
[ "$i" -lt 40 ] || { echo "loopback CDP tunnel did not start" >&2; exit 1; }

if [ "$mode" = office ]; then
  PC_INSTALLED_OFFICE_ONLY=1 PC_CHECK_PORT="$port" PC_INSTALLED_TEST_NSEC_FILE="$key" \
    "$repo/.venv/bin/python" "$repo/scripts/check_installed_desktop_account.py"
elif [ "$mode" = full ]; then
  PC_CHECK_PORT="$port" PC_INSTALLED_TEST_NSEC_FILE="$key" PC_INSTALLED_FIXTURE_DIR="$fixture" \
    "$repo/.venv/bin/python" "$repo/scripts/check_installed_desktop_account.py"
elif [ "$mode" = current ]; then
  PC_CHECK_PORT="$port" PC_INSTALLED_FIXTURE_DIR="$fixture" "$repo/.venv/bin/python" \
    "$repo/scripts/check_installed_desktop_account.py"
else
  PC_CHECK_PORT="$port" PC_INSTALLED_CODE_ROOT="$code_root" "$repo/.venv/bin/python" \
    "$repo/scripts/check_installed_code.py"
  PC_CHECK_PORT="$port" "$repo/.venv/bin/python" "$repo/scripts/check_installed_code_focus.py"
fi
