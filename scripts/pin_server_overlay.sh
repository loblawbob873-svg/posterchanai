#!/bin/bash
# Pin app-misc/posterchan-server in a STAGED overlay to the commit being published.
#
#   scripts/pin_server_overlay.sh <staged-overlay> <previously-published-overlay|""> <commit>
#
# Called by publish_overlay.sh after it has copied os/overlay into its staging tree. The server ebuild
# fetches a commit archive of the public mirror, so publishing it means three things moving together:
# the PC_COMMIT line, a NEW version (a changed ebuild under the same version is invisible to portage),
# and a Manifest with that archive's digest.
#
# BUT ONLY WHEN THE SERVER CHANGED. Every PosterChanOS machine carries this package, most with the
# server switched off, and a new version means every one of them downloads ~90 MB on its next update.
# So when nothing the package installs differs from the last published pin, the previously published
# ebuild and Manifest are carried over byte for byte and no machine re-downloads anything.
#
# NEVER THE REASON A PUBLISH FAILS. The desktop and the session are the urgent half of an overlay
# publish; if the archive for this commit cannot be fetched (the mirror push lagging, GitHub down), the
# last published server pin — or failing that the committed one, which has its own Manifest — stays.
set -euo pipefail

STAGE="${1:?staged overlay}"
PREV="${2:-}"
SHA="${3:?commit}"
REPO="${PC_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CURL="${PC_CURL:-curl}"
MIRROR="https://github.com/loblawbob873-svg/posterchanai/archive"
PKG="app-misc/posterchan-server"
DIR="$STAGE/$PKG"
# What the package installs — everything its ebuild does not delete — plus the package itself.
PATHS=(. ':(exclude)tests' ':(exclude)mobile' ':(exclude)desktop' ':(exclude)docs' ':(exclude)extension'
       ':(exclude)os' os/bin/pc-server "os/overlay/$PKG")

say(){ echo "[overlay] server: $*"; }
one_ebuild(){ find "$1" -maxdepth 1 -name 'posterchan-server-*.ebuild' -print 2>/dev/null | sort | tail -n1; }
pinned(){ sed -n 's/^PC_COMMIT="\([0-9a-f]\{40\}\)"$/\1/p' "$1" 2>/dev/null | head -n1; }
restore_prev(){
  rm -f "$DIR"/posterchan-server-*.ebuild "$DIR/Manifest"
  cp -p "$PREV_EB" "$PREV/$PKG/Manifest" "$DIR/"
}

[ -d "$DIR" ] || { say "no $PKG in the staged overlay — nothing to pin"; exit 0; }
EB="$(one_ebuild "$DIR")"
[ -n "$EB" ] || { echo "[overlay] ERROR: $PKG has no ebuild" >&2; exit 1; }

PREV_EB=""; PREV_SHA=""
if [ -n "$PREV" ] && [ -f "$PREV/$PKG/Manifest" ]; then
  PREV_EB="$(one_ebuild "$PREV/$PKG")"
  [ -n "$PREV_EB" ] && PREV_SHA="$(pinned "$PREV_EB")"
fi

if [ -n "$PREV_SHA" ] && git -C "$REPO" diff --quiet "$PREV_SHA" "$SHA" -- "${PATHS[@]}" 2>/dev/null; then
  restore_prev
  say "unchanged since ${PREV_SHA:0:12} — keeping $(basename "$PREV_EB")"
  exit 0
fi

VER="${PC_SERVER_VERSION:-1.0.$(date -u +%Y%m%d%H%M%S)}"
DL="$(mktemp)"
trap 'rm -f "$DL"' EXIT
if "$CURL" -fsSL --retry 2 --max-time 900 -o "$DL" "$MIRROR/$SHA.tar.gz" && [ -s "$DL" ]; then
  NEW="$DIR/posterchan-server-$VER.ebuild"
  sed "s/^PC_COMMIT=\".*\"$/PC_COMMIT=\"$SHA\"/" "$EB" >"$NEW.tmp"
  [ "$(pinned "$NEW.tmp")" = "$SHA" ] || { echo "[overlay] ERROR: could not re-pin PC_COMMIT" >&2; rm -f "$NEW.tmp"; exit 1; }
  rm -f "$DIR"/posterchan-server-*.ebuild
  mv "$NEW.tmp" "$NEW"
  printf 'DIST posterchan-server-%s.tar.gz %s BLAKE2B %s SHA512 %s\n' "$VER" "$(stat -c%s "$DL")" \
    "$(b2sum "$DL" | cut -d' ' -f1)" "$(sha512sum "$DL" | cut -d' ' -f1)" >"$DIR/Manifest"
  say "pinned ${SHA:0:12} as $VER"
  exit 0
fi

if [ -n "$PREV_SHA" ]; then
  restore_prev
  say "WARNING: could not fetch the archive for ${SHA:0:12}; keeping the published $(basename "$PREV_EB")"
else
  say "WARNING: could not fetch the archive for ${SHA:0:12}; shipping the committed pin $(pinned "$EB" | cut -c1-12)"
fi
exit 0
