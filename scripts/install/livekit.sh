#!/bin/bash
# Install the built-in LiveKit SFU for Concord community calls (add-on: ./install.sh --livekit; also part of a
# default install). One prebuilt Go binary, pinned AND checksummed; the app supervises it
# (app/services/livekit_service.py) and answers as the CORD-07 call broker (app/routers/concord_av.py).
# Absent binary -> Concord calls simply use another broker (armada.buzz); nothing breaks.
#
# Pinned because livekit_service.render_config targets these config keys (rtc.udp_port mux, turn_servers.secret).
# Bump LIVEKIT_VERSION together with the three sha256 values below, from the release's checksums.txt.

LIVEKIT_VERSION_PINNED="v1.13.9"
LIVEKIT_SHA256_AMD64="0b7fa208b662d09cfdeae8c06cf4c481aead0556086558b48250501e2e2d6e20"
LIVEKIT_SHA256_ARM64="c5e4e5a6d6cf0a449651aa3178532ecc7a2bf2502538396ebe6e51ff13424056"
LIVEKIT_SHA256_ARMV7="ef570f4898e8dc83ac303414fc0992afe3ac271cc9ceb57ed6baebb4fdee9f0a"

setup_livekit_server() {
    echo -e "${BOLD:-}🎙️ Installing the built-in LiveKit server for Concord calls${NC:-}"
    local repo_root dir ver arch sha m tarball url tmp got
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    dir="$repo_root/livekit"
    ver="$LIVEKIT_VERSION_PINNED"
    m="$(uname -m)"
    case "$m" in
        x86_64|amd64) arch="amd64"; sha="$LIVEKIT_SHA256_AMD64" ;;
        aarch64|arm64) arch="arm64"; sha="$LIVEKIT_SHA256_ARM64" ;;
        armv7l|armv7) arch="armv7"; sha="$LIVEKIT_SHA256_ARMV7" ;;
        *) echo "LiveKit: unsupported architecture $m"; return 1 ;;
    esac
    if [ -x "$dir/livekit-server" ] && "$dir/livekit-server" --version 2>/dev/null | grep -q "${ver#v}"; then
        echo "LiveKit $ver already installed"; return 0
    fi
    tarball="livekit_${ver#v}_linux_${arch}.tar.gz"
    url="https://github.com/livekit/livekit/releases/download/${ver}/${tarball}"
    tmp="$(mktemp -d)" || return 1
    echo "Downloading $url"
    if ! curl -fsSL "$url" -o "$tmp/$tarball"; then
        echo "LiveKit: download failed ($url)"; rm -rf "$tmp"; return 1
    fi
    got="$(sha256sum "$tmp/$tarball" | cut -d' ' -f1)"
    if [ "$got" != "$sha" ]; then
        echo "LiveKit: checksum mismatch for $tarball (got $got, want $sha) -- not installed"; rm -rf "$tmp"; return 1
    fi
    tar -xzf "$tmp/$tarball" -C "$tmp" livekit-server 2>/dev/null || tar -xzf "$tmp/$tarball" -C "$tmp"
    if [ ! -f "$tmp/livekit-server" ]; then
        echo "LiveKit: livekit-server not found in the archive"; rm -rf "$tmp"; return 1
    fi
    mkdir -p "$dir" && install -m 0755 "$tmp/livekit-server" "$dir/livekit-server"
    rm -rf "$tmp"
    echo "Installed $dir/livekit-server ($ver)"
    cat <<'NEXT'

To host Concord calls (Admin -> Live -> "Community Calls"):
  1. Router/firewall: forward UDP 7882 and TCP 7881 to this machine.
  2. nginx: proxy wss://<your host>/livekit/ to http://127.0.0.1:7880/ (WebSocket upgrade).
  3. Tick "Host Concord calls on this server" and Save (Public IP falls back to the TURN one).
NEXT
}
