#!/bin/bash
# Atomically publish a verified PosterChanOS image under the stable public filename.
set -euo pipefail

ISO="${1:-}"
PUBLISH_HOST="${PC_ISO_PUBLISH_HOST:-root@198.55.116.7}"
PUBLISH_PATH="${PC_ISO_PUBLISH_PATH:-/iso/posterchanos.iso}"
STAGING_PATH="${PUBLISH_PATH}.uploading"

if [[ -z "$ISO" || ! -f "$ISO" || ! -s "$ISO" ]]; then
	echo "Usage: $0 /absolute/path/to/posterchan-live-YYYYMMDD.iso" >&2
	exit 2
fi
if [[ "$ISO" != /* ]]; then
	echo "Refusing a relative ISO path: $ISO" >&2
	exit 2
fi
if [[ "$PUBLISH_PATH" != /* || "$PUBLISH_PATH" == "/" ]]; then
	echo "Refusing unsafe publish path: $PUBLISH_PATH" >&2
	exit 2
fi
# Both values are embedded in scp/remote-shell arguments. Keep optional overrides useful for a
# staging server without letting whitespace or shell punctuation become remote commands.
if [[ ! "$PUBLISH_HOST" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+$ ]]; then
	echo "Refusing unsafe publish host: $PUBLISH_HOST" >&2
	exit 2
fi
if [[ ! "$PUBLISH_PATH" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
	echo "Refusing unsafe publish path: $PUBLISH_PATH" >&2
	exit 2
fi
PUBLISH_DIR="${PUBLISH_PATH%/*}"

LOCAL_SHA="$(sha256sum "$ISO" | awk '{print $1}')"
echo "Publishing $ISO to $PUBLISH_HOST:$PUBLISH_PATH"

# Readers keep receiving the previous complete image until the upload and checksum both succeed.
ssh "$PUBLISH_HOST" "mkdir -p '$PUBLISH_DIR' && rm -f '$STAGING_PATH'"
scp -- "$ISO" "$PUBLISH_HOST:$STAGING_PATH"
REMOTE_SHA="$(ssh "$PUBLISH_HOST" "sha256sum '$STAGING_PATH'" | awk '{print $1}')"
if [[ "$REMOTE_SHA" != "$LOCAL_SHA" ]]; then
	ssh "$PUBLISH_HOST" "rm -f '$STAGING_PATH'" || true
	echo "ISO checksum mismatch: local $LOCAL_SHA, remote $REMOTE_SHA" >&2
	exit 1
fi
ssh "$PUBLISH_HOST" "chmod 0644 '$STAGING_PATH' && mv -f '$STAGING_PATH' '$PUBLISH_PATH'"
echo "Published $PUBLISH_HOST:$PUBLISH_PATH (sha256 $LOCAL_SHA)"
