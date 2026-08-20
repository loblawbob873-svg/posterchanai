#!/bin/bash
# Publish the PosterChanOS overlay so installed machines can update themselves.
#
# WHERE IT GOES, AND WHY THERE. router.lan already NFS-mounts 192.168.0.85:/raid/distfiles and
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
DEST="${DEST:-/raid/distfiles/posterchan-overlay.git}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/os/overlay"
URL="https://gentoo.poster.place/posterchan-overlay.git"

[ -d "$SRC" ] || { echo "no overlay at $SRC" >&2; exit 1; }
[ -f "$SRC/profiles/repo_name" ] || { echo "$SRC is not a portage repository" >&2; exit 1; }

echo "[overlay] staging $(find "$SRC" -type f | wc -l) files"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cp -r "$SRC/." "$TMP/"

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
ssh "$NAS" "cd $DEST && git update-server-info && git config core.sharedRepository group && chmod -R a+rX ."

echo "[overlay] published"
echo "[overlay]   sync-uri = $URL"
echo "[overlay] verify with:  git ls-remote $URL"
