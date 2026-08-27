#!/bin/bash
# Read back the boot-critical/core-app payload from an installed PosterChanOS hard drive.
set -euo pipefail

desktop="$(qlist -Iv app-misc/posterchan-desktop 2>/dev/null || true)"
shell="$(qlist -Iv app-misc/posterchanos-shell 2>/dev/null || true)"
[ -n "$desktop" ] || { echo "Portage does not own the installed desktop" >&2; exit 1; }
[ -n "$shell" ] || { echo "Portage does not own the installed PosterChanOS session" >&2; exit 1; }

executables=(
  /opt/posterchan/posterchan-desktop
  /opt/posterchan/resources/tor/tor/tor
  /usr/local/bin/posterchan
  /usr/local/bin/update-posterchan
  /usr/local/bin/pc-shell-start
  /usr/local/bin/pc-shell-restart
  /usr/local/bin/pc-provision-user
  /usr/local/bin/pc-session-switch
  /usr/local/bin/pc-window-cycle
  /usr/local/bin/pc-window-snap
  /usr/local/bin/pc-key
  /usr/local/bin/pc-idle
  /usr/local/bin/pc-screenshot
  /usr/bin/gentoo.sh
)
for file in "${executables[@]}"; do
  [ -x "$file" ] || { echo "installed executable is missing or mode-stripped: $file" >&2; exit 1; }
  qfile -q "$file" >/dev/null 2>&1 || { echo "installed executable is not package-owned: $file" >&2; exit 1; }
done

[ "$(stat -c %a /opt/posterchan/chrome-sandbox)" = 4755 ] || {
  echo "Electron sandbox helper is not setuid 4755" >&2; exit 1;
}
[ "$(stat -c %a /etc/sudoers.d/posterchan-provision)" = 440 ] || {
  echo "provision sudo rule has an unsafe mode" >&2; exit 1;
}
[ "$(stat -c %a /etc/sudoers.d/posterchan-session-switch)" = 440 ] || {
  echo "session-switch sudo rule has an unsafe mode" >&2; exit 1;
}

for command in sway foot firefox-bin virsh qemu-system-x86_64; do
  command -v "$command" >/dev/null || { echo "first-run core command is missing: $command" >&2; exit 1; }
done
grep -q 'posterchan-update.lock' /usr/local/bin/update-posterchan
grep -q 'emaint sync -r posterchan' /usr/local/bin/update-posterchan
autologin_user="$(sed -n 's/^ExecStart=.*--autologin \([^ ]*\).*/\1/p' \
  /etc/systemd/system/getty@tty1.service.d/override.conf)"
[ -n "$autologin_user" ] && getent passwd "$autologin_user" >/dev/null || {
  echo "tty1 autologin does not name an installed account" >&2; exit 1;
}
autologin_home="$(getent passwd "$autologin_user" | cut -d: -f6)"
grep -q 'exec sway' "$autologin_home/.bash_profile"

printf 'Installed core package gate passed: %s; %s; autologin %s\n' \
  "$desktop" "$shell" "$autologin_user"
