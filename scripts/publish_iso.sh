#!/bin/bash
# Publish a GATED PosterChanOS image as https://iso.poster.place/posterchanos.iso.
set -euo pipefail

ISO="${1:-}"
# iso.poster.place IS THE CUSTOM DOMAIN OF THE CLOUDFLARE R2 BUCKET `posterchan` (since 2026-09-25), so
# the image is served by Cloudflare and nothing at home spends upload bandwidth on 4 GB downloads. It
# used to live on router.lan's disk (/srv/iso), before that in nas's distfiles export (whose Gentoo
# mirror sync --delete removed it overnight), and before that on a VPS (198.55.116.7, deleted
# 2026-09-21). publish_r2.py uploads to a staging key, verifies it, and only then replaces the public
# object, so a failure at any step leaves the previous image serving.
#
# Credentials never touch the repo: ~/.config/posterchan/{cloudflare.account,r2.access_key_id,
# r2.secret_access_key} (mode 600). Without them this exits 2 having sent nothing.
PUBLIC_URL="${PC_ISO_PUBLIC_URL:-https://iso.poster.place/posterchanos.iso}"

if [[ -z "$ISO" || ! -f "$ISO" || ! -s "$ISO" ]]; then
	echo "Usage: $0 /absolute/path/to/posterchan-live-YYYYMMDD.iso" >&2
	exit 2
fi
if [[ "$ISO" != /* ]]; then
	echo "Refusing a relative ISO path: $ISO" >&2
	exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
R2_PY="$HERE/publish_r2.py"
PY="$HERE/../venv-unified/bin/python"
[[ -x "$PY" ]] || PY=python3
if [[ ! -f "$R2_PY" ]]; then
	echo "The R2 publisher is missing beside this script: $R2_PY" >&2
	exit 2
fi

LOCAL_SHA="$(sha256sum "$ISO" | cut -d' ' -f1)"
LOCAL_SIZE="$(stat -c %s "$ISO")"
echo "Publishing $ISO ($LOCAL_SIZE bytes, sha256 $LOCAL_SHA) to $PUBLIC_URL"
rc=0
"$PY" "$R2_PY" "$ISO" || rc=$?
if [[ $rc -ne 0 ]]; then
	echo "NOT published (publish_r2.py exited $rc) -- $PUBLIC_URL still serves the previous image" >&2
	exit "$rc"
fi

# READ BACK WHAT THE INTERNET GETS, not what the upload said. R2 verified its own object; this checks the
# public name end to end -- the custom domain, the edge, and DNS. On the LAN iso.poster.place used to be
# answered by router.lan's wildcard rewrite, which would "verify" an image nobody outside can see.
PUBLISHED_SHA="$(curl -fsS --max-time 60 -H 'Cache-Control: no-cache' "$PUBLIC_URL.sha256" | cut -d' ' -f1 || true)"
PUBLISHED_SIZE="$(curl -fsSI --max-time 60 -H 'Cache-Control: no-cache' "$PUBLIC_URL" \
	| tr -d '\r' | sed -n 's/^[Cc]ontent-[Ll]ength: *//p' | tail -1 || true)"
if [[ -z "$PUBLISHED_SHA" || -z "$PUBLISHED_SIZE" ]]; then
	# The R2 object IS replaced by now; say so rather than implying the upload failed.
	echo "Published to R2, but $PUBLIC_URL could not be read back (checksum '${PUBLISHED_SHA}', size '${PUBLISHED_SIZE}')" >&2
	exit 1
fi
if [[ "$PUBLISHED_SHA" != "$LOCAL_SHA" || "$PUBLISHED_SIZE" != "$LOCAL_SIZE" ]]; then
	echo "$PUBLIC_URL serves a different image: sha256 $PUBLISHED_SHA / $PUBLISHED_SIZE bytes, local $LOCAL_SHA / $LOCAL_SIZE" >&2
	exit 1
fi
echo "Published $PUBLIC_URL and $PUBLIC_URL.sha256 (sha256 $LOCAL_SHA)"
