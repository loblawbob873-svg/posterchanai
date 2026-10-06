#!/bin/bash
# Wait for THIS commit's Android APK build before mirroring it to poster.place/apk.
#
# sync.sh used to refresh on fixed delays (4, 6 and 10 minutes). The APK workflow now waits for the
# emulator checks first -- 20 to 45 minutes -- so all three refreshes fetched the PREVIOUS build, and /apk
# (and the in-app updater, which reads /apk/version) sat a whole release behind: deploy 104 published
# build 2472 at 23:42 while /apk still served 2469 from 19:19.
#
# Exits 0 when the build for <sha> succeeded, or when no Android run appears for it within a few minutes
# (a deploy that touched no client file builds no APK -- the refresh is then a harmless no-op). Exits 1
# when that build failed or never finished, so nothing is mirrored for it.
set -u
sha=${1:?usage: wait_apk_build.sh <commit-sha>}
REPO=${POSTERCHAN_APK_REPO:-loblawbob873-svg/posterchanai}
POLL=${PC_APK_POLL:-60}           # seconds between looks
LIMIT=${PC_APK_LIMIT:-75}         # looks before giving up (~75 min)
NONE=${PC_APK_NONE:-8}            # looks with no run at all before deciding none is coming
command -v gh >/dev/null || { echo "wait_apk_build: no gh -- not waiting"; exit 0; }
for i in $(seq 1 "$LIMIT"); do
  st=$(gh run list --repo "$REPO" --workflow android.yml --commit "$sha" --limit 1 \
         --json status,conclusion --jq '.[0] | "\(.status) \(.conclusion)"' 2>/dev/null)
  case "$st" in
    "completed success") echo "wait_apk_build: ${sha:0:9} built"; exit 0 ;;
    completed\ *) echo "wait_apk_build: ${sha:0:9} build ${st#completed } -- nothing to mirror"; exit 1 ;;
    "" | "null null") [ "$i" -ge "$NONE" ] && { echo "wait_apk_build: no Android build for ${sha:0:9}"; exit 0; } ;;
  esac
  sleep "$POLL"
done
echo "wait_apk_build: ${sha:0:9} still building after $LIMIT looks -- giving up"; exit 1
