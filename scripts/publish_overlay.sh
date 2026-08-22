#!/bin/bash
# Publish the PosterChanOS overlay so installed machines can update themselves.
#
# WHERE IT GOES, AND WHY THERE. router.lan NFS-mounts the explicitly exported
# 192.168.0.85:/raid/distfiles/distfiles at /var/lib/distfiles and
# already serves it as https://gentoo.poster.place with autoindex and a valid certificate. Putting
# the overlay inside that tree needs no new vhost, no new certificate and no new mount — and it is
# the same /raid the Gentoo mirror lives on, which is where this was asked to go.
#
# SERVED AS A GIT REPO, not as a directory of files. Portage can sync a repo over plain HTTP only if
# it is a git repository — `sync-type = git` — and a "dumb" HTTP clone works from any web server
# that can serve the object files, provided `update-server-info` has been run. That is one command
# per publish and it is the difference between a URL portage can use and one it cannot.
set -euo pipefail

NAS="${NAS:-nas.lan}"
DEST="${DEST:-/raid/distfiles/distfiles/posterchan-overlay.git}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/os/overlay"
URL="https://gentoo.poster.place/posterchan-overlay.git"

[ -d "$SRC" ] || { echo "no overlay at $SRC" >&2; exit 1; }
[ -f "$SRC/profiles/repo_name" ] || { echo "$SRC is not a portage repository" >&2; exit 1; }

# THE EBUILD VERSION MUST FOLLOW THE DESKTOP BUILD, or the overlay is a package manager pointed at
# a rolling URL: SRC_URI always fetches the newest AppImage, portage sees the same version number it
# already has installed, and `emerge -u` reports nothing to do — for ever. The version is read from
# the update feed the desktop app itself uses, so there is one source of truth for "what is current".
# The website route is intentionally not a directory listing and may return 404. The rolling
# GitHub release is the artifact source used below, so read its Linux update manifest directly.
LIVE=$(curl -sSfL --max-time 20 \
       https://github.com/loblawbob873-svg/posterchanai/releases/download/desktop-latest/latest-linux.yml \
       2>/dev/null | sed -n 's/^version: *//p' | head -1)
if [ -n "${LIVE:-}" ]; then
    EB_DIR="$SRC/app-misc/posterchan-desktop"
    CUR=$(ls "$EB_DIR"/posterchan-desktop-*.ebuild 2>/dev/null | head -1)
    if [ -n "$CUR" ] && [ "$(basename "$CUR")" != "posterchan-desktop-${LIVE}.ebuild" ]; then
        echo "[overlay] desktop build is $LIVE (ebuild was $(basename "$CUR" .ebuild | sed 's/.*-//'))"
        git mv "$CUR" "$EB_DIR/posterchan-desktop-${LIVE}.ebuild" 2>/dev/null \
            || mv "$CUR" "$EB_DIR/posterchan-desktop-${LIVE}.ebuild"
    fi
else
    echo "[overlay] could not read the desktop version — publishing the ebuild as it stands" >&2
fi

# ---------------------------------------------------------------- the Manifest
#
# WITHOUT ONE, PORTAGE REFUSES THE BINARY IT JUST DOWNLOADED: "VERIFY FAILED! Reason: Insufficient
# data for checksum verification". That is not a corrupt download, it is an ebuild with nothing to
# check against -- and it is what every `update-posterchan` on an overlay machine hit.
#
# Generated here rather than committed, because it describes bytes that live at a URL: the digest and
# the ebuild version have to move together, and this is the one place that already knows both (it
# renamed the ebuild to the live version a few lines up).
#
# The hashes are the ones the overlay's layout.conf declares. A thin-manifest DIST line is exactly
# size + those digests, so it is written directly rather than through `ebuild ... manifest`, which
# would need a configured portage tree and tie publishing to this machine's package state.
if [ -n "${LIVE:-}" ]; then
    EB_DIR="$SRC/app-misc/posterchan-desktop"
    ASSET="PosterChan-${LIVE}-linux-x64.tar.zst"
    GH="https://github.com/loblawbob873-svg/posterchanai/releases/download/desktop-latest"
    DL="$(mktemp)"
    if curl -fsSL --retry 2 --max-time 900 -o "$DL" "$GH/$ASSET"; then
        printf 'DIST posterchan-desktop-%s.tar.zst %s BLAKE2B %s SHA512 %s\n' \
            "$LIVE" "$(stat -c%s "$DL")" \
            "$(b2sum "$DL" | cut -d' ' -f1)" \
            "$(sha512sum "$DL" | cut -d' ' -f1)" >"$EB_DIR/Manifest"
        echo "[overlay] Manifest written for $ASSET"
    else
        # NEVER LEAVE A STALE ONE. A Manifest describing a different build is worse than none: the
        # download succeeds and portage rejects it, which reads as a corrupt mirror.
        rm -f "$EB_DIR/Manifest"
        echo "[overlay] WARN: could not fetch $ASSET — publishing with no Manifest (emerge will refuse it)" >&2
    fi
    rm -f "$DL"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cp -r "$SRC/." "$TMP/"

# ONE CANONICAL INSTALLER. Keeping a second 3,000-line gentoo.sh under FILESDIR guarantees drift;
# instead inject the repository's os/gentoo.sh into the staging tree that becomes the real overlay.
# The ebuild owns /usr/bin/gentoo.sh, so every ordinary update now refreshes the LiveUSB/repair tool.
install -m 0755 "$(dirname "$SRC")/gentoo.sh" \
  "$TMP/app-misc/posterchanos-shell/files/gentoo.sh"

# A changed ebuild with the same version is invisible to Portage. The shell used to stay 1.0.0 for
# ever, which is why installed machines said “Already up to date” while keeping an old launcher.
# Timestamp versions are monotonic and make each published session (helpers + gentoo.sh) upgradable.
SHELL_DIR="$TMP/app-misc/posterchanos-shell"
SHELL_EBUILD=$(find "$SHELL_DIR" -maxdepth 1 -name 'posterchanos-shell-*.ebuild' -print -quit)
SHELL_VER="1.0.$(date -u +%Y%m%d%H%M%S)"
mv "$SHELL_EBUILD" "$SHELL_DIR/posterchanos-shell-${SHELL_VER}.ebuild"

echo "[overlay] staging $(find "$TMP" -type f | wc -l) files (shell $SHELL_VER)"

cd "$TMP"
git init -q -b main
git add -A
git -c user.email=os@poster.place -c user.name=PosterChanOS \
    commit -q -m "overlay $(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "[overlay] publishing to $NAS:$DEST"
ssh "$NAS" "mkdir -p $DEST && cd $DEST && (git rev-parse --git-dir >/dev/null 2>&1 || git init -q --bare)"
git push -q --force "ssh://$NAS$DEST" main
# Dumb HTTP needs this, and it is the step whose absence looks like a working publish: the files are
# all there, the URL returns 200 for the directory, and `emerge --sync` says the repo is empty.
# HEAD MUST NAME THE BRANCH WE PUSH. `git init --bare` points HEAD at refs/heads/master; pushing
# `main` leaves HEAD dangling, and a clone then succeeds, reports "remote HEAD refers to nonexistent
# ref", and produces an EMPTY working tree. `git ls-remote` shows the branch perfectly the whole
# time, so the repo looks published from every angle except the one that matters.
ssh "$NAS" "cd $DEST && git symbolic-ref HEAD refs/heads/main && git update-server-info \
    && git config core.sharedRepository group && chmod -R a+rX ."

echo "[overlay] published"
echo "[overlay]   sync-uri = $URL"
echo "[overlay] verify with:  git ls-remote $URL"
