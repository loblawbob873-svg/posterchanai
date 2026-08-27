#!/bin/sh
# Exercise File Manager/open-with code from the installed Gentoo ASAR, not the source checkout.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
asar=${PC_INSTALLED_ASAR:-/opt/posterchan/resources/app.asar}
asar_cli=${PC_ASAR_CLI:-$repo/desktop/node_modules/@electron/asar/bin/asar.js}
[ -r "$asar" ] || { echo "installed ASAR not readable: $asar" >&2; exit 1; }
[ -r "$asar_cli" ] || { echo "ASAR reader not found: $asar_cli" >&2; exit 1; }

check_dir=$(mktemp -d /tmp/pc-installed-open-with.XXXXXX)
cleanup() { rm -r "$check_dir"; }
trap cleanup EXIT HUP INT TERM

extract() {
  member=$1
  out=$2
  # extract-file writes the member's basename into cwd.
  (cd "$check_dir" && node "$asar_cli" extract-file "$asar" "$member")
  mv "$check_dir/$(basename "$member")" "$out"
}

extract www/static/js/client/app.js "$check_dir/installed-app.js"
extract www/static/js/client/hostfiles.js "$check_dir/installed-hostfiles.js"
extract www/static/js/client/preview.js "$check_dir/installed-preview.js"

PC_INSTALLED_APP_JS="$check_dir/installed-app.js" \
  node "$repo/tests/client/open_with_selector_sim.js"
PC_INSTALLED_APP_JS="$check_dir/installed-app.js" \
  node "$repo/tests/client/folder_drop_paths_sim.js"
PC_INSTALLED_APP_JS="$check_dir/installed-app.js" \
  node "$repo/tests/client/folder_upload_completion_sim.js"
PC_INSTALLED_HOSTFILES_JS="$check_dir/installed-hostfiles.js" \
  node "$repo/tests/client/hostfiles_click_sim.js"
PC_INSTALLED_PREVIEW_JS="$check_dir/installed-preview.js" \
  node "$repo/tests/client/preview_sim.js"
echo "OK installed File Manager open-with routes"
