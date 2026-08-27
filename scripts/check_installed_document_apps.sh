#!/bin/sh
# Exercise Office workspace wiring and Email layout/attachment URLs from the immutable installed ASAR.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
asar=${PC_INSTALLED_ASAR:-/opt/posterchan/resources/app.asar}
asar_cli=${PC_ASAR_CLI:-$repo/desktop/node_modules/@electron/asar/bin/asar.js}
[ -r "$asar" ] || { echo "installed ASAR not readable: $asar" >&2; exit 1; }
[ -r "$asar_cli" ] || { echo "ASAR reader not found: $asar_cli" >&2; exit 1; }

check_dir=$(mktemp -d /tmp/pc-installed-document-apps.XXXXXX)
cleanup() { rm -r "$check_dir"; }
trap cleanup EXIT HUP INT TERM
(cd "$check_dir" && node "$asar_cli" extract-file "$asar" www/static/js/client/app.js)
mv "$check_dir/app.js" "$check_dir/installed-app.js"
(cd "$check_dir" && node "$asar_cli" extract-file "$asar" www/static/js/client/os.js)
mv "$check_dir/os.js" "$check_dir/installed-os.js"

PC_INSTALLED_APP_JS="$check_dir/installed-app.js" PC_INSTALLED_OS_JS="$check_dir/installed-os.js" \
  node "$repo/tests/client/installed_document_workspace_sim.js"
PC_INSTALLED_APP_JS="$check_dir/installed-app.js" \
  "$repo/.venv/bin/python" "$repo/scripts/check_mail_mobile.py"
