#!/bin/bash
# Atomically mirror the signed rolling APK. The version comes from the APK itself, never release
# prose: a manually replaced asset with stale notes previously left /apk serving build 1680 while
# GitHub already held 1694.
set -euo pipefail

DEST=${POSTERCHAN_APK_DIR:-/home/verita84/posterchan-apk}
REPO=${POSTERCHAN_APK_REPO:-loblawbob873-svg/posterchanai}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$DEST"
tmp=$(mktemp "$DEST/posterchan.apk.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT

curl -fsSL --retry 3 --max-time 300 -o "$tmp" \
  "https://github.com/$REPO/releases/download/apk-latest/posterchan.apk"
build=$(python3 "$ROOT/scripts/apk_embedded_build.py" "$tmp")
current=0
if [ -s "$DEST/posterchan.apk" ]; then
  current=$(python3 "$ROOT/scripts/apk_embedded_build.py" "$DEST/posterchan.apk" 2>/dev/null || echo 0)
fi
if [ "$build" -lt "$current" ]; then
  echo "refresh: refusing APK downgrade $current -> $build" >&2
  exit 1
fi
if [ "$build" -eq "$current" ] && cmp -s "$tmp" "$DEST/posterchan.apk"; then
  echo "refresh: build $build already mirrored"
  exit 0
fi
mv -f -- "$tmp" "$DEST/posterchan.apk"
trap - EXIT
printf '%s\n' "$build" > "$DEST/version.txt"
echo "apk refreshed: build $build ($(stat -c%s "$DEST/posterchan.apk") bytes)"
