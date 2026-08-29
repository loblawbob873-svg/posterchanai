#!/bin/sh
# Exercise native-window identity from the immutable installed ASAR without opening a GUI window.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
asar=${PC_INSTALLED_ASAR:-/opt/posterchan/resources/app.asar}
asar_cli=${PC_ASAR_CLI:-$repo/desktop/node_modules/@electron/asar/bin/asar.js}
[ -r "$asar" ] || { echo "installed ASAR not readable: $asar" >&2; exit 1; }
[ -r "$asar_cli" ] || { echo "ASAR reader not found: $asar_cli" >&2; exit 1; }

check_dir=$(mktemp -d /tmp/pc-installed-wm-package.XXXXXX)
cleanup() { rm -r "$check_dir"; }
trap cleanup EXIT HUP INT TERM
(cd "$check_dir" && node "$asar_cli" extract-file "$asar" wm.js)
mv "$check_dir/wm.js" "$check_dir/installed-wm.js"
(cd "$check_dir" && node "$asar_cli" extract-file "$asar" clipboard.js)
mv "$check_dir/clipboard.js" "$check_dir/installed-clipboard.js"
(cd "$check_dir" && node "$asar_cli" extract-file "$asar" www/static/js/client/os.js)
mv "$check_dir/os.js" "$check_dir/installed-os.js"
(cd "$check_dir" && node "$asar_cli" extract-file "$asar" www/static/css/client.css)
mv "$check_dir/client.css" "$check_dir/installed-client.css"
PC_INSTALLED_WM_JS="$check_dir/installed-wm.js" \
  node "$repo/tests/client/installed_wm_ancestry_sim.js"
PC_INSTALLED_CLIPBOARD_JS="$check_dir/installed-clipboard.js" \
  node "$repo/tests/client/installed_clipboard_sim.js"
PC_INSTALLED_OS_JS="$check_dir/installed-os.js" \
  node "$repo/tests/client/alt_tab_switcher_sim.js"
PC_INSTALLED_OS_JS="$check_dir/installed-os.js" PC_INSTALLED_CLIENT_CSS="$check_dir/installed-client.css" \
  node "$repo/tests/client/installed_alt_tab_cross_output_sim.js"
