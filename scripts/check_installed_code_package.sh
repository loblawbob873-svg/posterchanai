#!/bin/sh
# Execute the native Git implementation from the immutable installed ASAR, never the checkout copy.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
asar=${PC_INSTALLED_ASAR:-/opt/posterchan/resources/app.asar}
asar_cli=${PC_ASAR_CLI:-$repo/desktop/node_modules/@electron/asar/bin/asar.js}
[ -r "$asar" ] || { echo "installed ASAR not readable: $asar" >&2; exit 1; }
[ -r "$asar_cli" ] || { echo "ASAR reader not found: $asar_cli" >&2; exit 1; }

check_dir=$(mktemp -d /tmp/pc-installed-code-package.XXXXXX)
cleanup() { rm -r "$check_dir"; }
trap cleanup EXIT HUP INT TERM
(cd "$check_dir" && node "$asar_cli" extract-file "$asar" hostfs.js)
mv "$check_dir/hostfs.js" "$check_dir/installed-hostfs.js"
(cd "$check_dir" && node "$asar_cli" extract-file "$asar" www/static/js/client/code.js)
mv "$check_dir/code.js" "$check_dir/installed-code.js"
PC_INSTALLED_HOSTFS_JS="$check_dir/installed-hostfs.js" \
  node "$repo/tests/client/installed_git_restore_sim.js"
# The browser harness can run beside source/package gates in CI. A fixed profile and debugging port
# lets one Chrome kill or replace the other's DevTools socket, which looks like a Code regression
# (`ConnectionClosedError`) after navigation. Give this exact package run both resources privately.
check_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
PC_INSTALLED_CODE_JS="$check_dir/installed-code.js" \
PC_CHECK_PROFILE="$check_dir/chrome-profile" \
PC_CHECK_PORT="$check_port" \
  "$repo/.venv/bin/python" "$repo/scripts/check_code_editor.py"
