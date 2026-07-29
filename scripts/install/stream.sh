#!/bin/bash
# Install the built-in MediaMTX media server for OBS streaming (add-on: ./install.sh --stream).
# MediaMTX is a single prebuilt Go binary (no build step); the app supervises it as a subprocess
# (app/services/stream_service.py) — no systemd unit. Absent binary → the streaming feature is a no-op.
#
# Save-to-Blossom recording (stream_record_enabled): MediaMTX records each stream as fmp4 into a temp dir
# (stream_record_dir, default /tmp/posterchanai-streams) which the app creates on demand, then uploads the
# finished VOD to Blossom and deletes it. Nothing to install here — it's just a directory; mount it as tmpfs
# (or point it at /dev/shm) if you want RAM-backed recording. No extra packages: ffprobe/ffmpeg cover it.
#
# Bitrate clamp (stream_clamp_enabled, ON by default): MediaMTX only remuxes, so without the clamp whatever
# OBS sends is what EVERY viewer downloads — a 6 Mbps stream costs 6 Mbps of upload per viewer. The clamp
# re-encodes each stream to 720p30 @ 1500k and serves viewers only that. It needs **ffmpeg** (checked by
# deps.sh) and uses the GPU's media engine when there is one — NVENC/VAAPI are auto-detected, else CPU.
# No new port to forward: it reads and writes over RTSP bound to 127.0.0.1. If ffmpeg is missing the
# transcode simply never starts and viewers get the unclamped source, so nothing breaks — it just costs
# far more bandwidth. Turn it off in Admin → Live → OBS Streaming.

setup_stream_server() {
    print_banner 2>/dev/null || true
    echo -e "${BOLD:-}🔴 Installing the built-in media server (MediaMTX) for OBS streaming${NC:-}"
    echo ""

    local repo_root stream_dir
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    stream_dir="$repo_root/streamserver"
    mkdir -p "$stream_dir"

    # Pinned version — the generated mediamtx config (stream_service._write_config) targets these exact
    # config keys, so we pin rather than track "latest" (config keys drift between MediaMTX releases).
    # Override with MEDIAMTX_VERSION only if you also verify the config still parses.
    local ver
    ver="${MEDIAMTX_VERSION:-v1.19.2}"

    # Map uname → MediaMTX asset arch.
    local os arch m
    os="linux"; m="$(uname -m)"
    case "$m" in
        x86_64|amd64) arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        armv7l|armv7)  arch="armv7" ;;
        *) print_error "unsupported arch: $m" 2>/dev/null || echo "ERROR: unsupported arch: $m"; return 1 ;;
    esac
    [ "$(uname -s)" = "Darwin" ] && os="darwin"

    local tarball url tmp
    tarball="mediamtx_${ver}_${os}_${arch}.tar.gz"
    url="https://github.com/bluenviron/mediamtx/releases/download/${ver}/${tarball}"
    tmp="$(mktemp -d)"
    echo "Downloading $url"
    if ! curl -fsSL "$url" -o "$tmp/$tarball"; then
        print_error "download failed ($url)" 2>/dev/null || echo "ERROR: download failed"
        echo "Set MEDIAMTX_VERSION to a valid tag from https://github.com/bluenviron/mediamtx/releases and retry."
        rm -rf "$tmp"; return 1
    fi
    tar -xzf "$tmp/$tarball" -C "$tmp" mediamtx 2>/dev/null || tar -xzf "$tmp/$tarball" -C "$tmp"
    if [ ! -f "$tmp/mediamtx" ]; then
        print_error "mediamtx binary not found in archive" 2>/dev/null || echo "ERROR: binary missing"
        rm -rf "$tmp"; return 1
    fi
    install -m 0755 "$tmp/mediamtx" "$stream_dir/mediamtx"
    rm -rf "$tmp"
    print_success "Installed $stream_dir/mediamtx ($ver)" 2>/dev/null || echo "OK: installed mediamtx $ver"

    cat <<'EOF'

Next steps to turn it on (Admin → Live → "OBS Streaming"):
  1. Firewall/router: forward to this machine —
       - TCP 1935            (RTMP ingest from OBS)
       - UDP 8189            (WebRTC media — needed for "Go live from phone" over the internet)
     For many viewers, also forward the HLS port (default 8888) and set "HLS base URL" to a grey-clouded
     stream.<domain>; otherwise the app reverse-proxies HLS over your existing tunnel (fine for a handful).
  2. In Admin: tick "Run the built-in media server", set "Stream domain" (host OBS/phones reach), Save.
     The app starts + supervises MediaMTX automatically (no restart needed).
  3. In the web client → Discover → Streams → "Go Live":
       - Phone: tap "Go live from this phone" to stream straight from the camera (WebRTC/WHIP), OR
       - OBS: copy the Server + Stream key, start OBS, then tap "Announce".
     Optionally announce to followers with a watch link.
EOF
}
