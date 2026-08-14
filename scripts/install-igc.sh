#!/bin/bash
# install-igc.sh - Install Intel Graphics Compiler (IGC) 2.35.5 system-wide.
#
# WHY: On Intel Arc this single IGC version unblocks BOTH GPU services:
#   * Image gen (SDXL >=768x768) - older IGC fails with oneDNN "could not create a primitive".
#   * LLM 14B / long-context - older IGC lacks the __spirv_GroupBroadcast SPIR-V builtin.
# Gentoo/most distros only ship up to 2.35.2, so we install the upstream 2.35.5 .so files
# directly into the system lib dir (with a backup), distro-agnostically.
#
# SOURCES (in priority order):
#   1) A local staged dir (default /opt/igc-2.35.5) - fastest, offline.
#   2) Upstream GitHub release v2.35.5 debs (intel-igc-core-2 + intel-igc-opencl-2), extracted.
#
# Usage:  sudo ./scripts/install-igc.sh            # auto: staged dir, else download
#         sudo ./scripts/install-igc.sh --download  # force download from GitHub
#         IGC_STAGE=/path sudo ./scripts/install-igc.sh
set -euo pipefail

IGC_VER="2.35.5"
IGC_TAG="v${IGC_VER}"
STAGE="${IGC_STAGE:-/opt/igc-${IGC_VER}}"
FORCE_DOWNLOAD=0
[ "${1:-}" = "--download" ] && FORCE_DOWNLOAD=1

# Pick the system lib dir (Gentoo/RH multilib -> lib64; Debian/Ubuntu -> the multiarch triplet).
if [ -d /usr/lib64 ]; then LIBDIR=/usr/lib64
elif [ -d /usr/lib/x86_64-linux-gnu ]; then LIBDIR=/usr/lib/x86_64-linux-gnu
else LIBDIR=/usr/lib; fi

if [ "$(id -u)" != "0" ]; then echo "ERROR: run as root (sudo)." >&2; exit 1; fi
echo "IGC ${IGC_VER} -> ${LIBDIR}"

# --- obtain the libs into a temp work dir -----------------------------------------------------
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
SRC=""

if [ "$FORCE_DOWNLOAD" = "0" ] && [ -d "$STAGE" ] && ls "$STAGE"/libigc.so.* >/dev/null 2>&1; then
    echo "Using staged libs in $STAGE"
    SRC="$STAGE"
else
    echo "Downloading IGC ${IGC_TAG} debs from GitHub..."
    command -v curl >/dev/null || { echo "ERROR: curl required for --download." >&2; exit 1; }
    API="https://api.github.com/repos/intel/intel-graphics-compiler/releases/tags/${IGC_TAG}"
    # Grab the core-2 and opencl-2 amd64 .deb asset URLs from the release.
    urls="$(curl -fsSL "$API" | grep -oE 'https://[^"]*(core-2|opencl-2)[^"]*_amd64\.deb' | sort -u)"
    [ -n "$urls" ] || { echo "ERROR: could not find core-2/opencl-2 debs in $IGC_TAG." >&2; exit 1; }
    for u in $urls; do
        echo "  $u"; curl -fsSL "$u" -o "$WORK/$(basename "$u")"
    done
    # Extract each .deb (ar -> data.tar.*) without dpkg, so this works on any distro.
    cd "$WORK"
    for deb in *.deb; do
        ar x "$deb"
        tar xf data.tar.* 2>/dev/null && rm -f data.tar.* control.tar.* debian-binary
    done
    SRC="$(dirname "$(find "$WORK" -name 'libigc.so.*' | head -1)")"
    [ -n "$SRC" ] || { echo "ERROR: no libigc.so.* in extracted debs." >&2; exit 1; }
    cd - >/dev/null
fi

# --- back up any existing IGC, then install ---------------------------------------------------
BACKUP="/opt/igc-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
shopt -s nullglob
for f in "$LIBDIR"/libigc.so* "$LIBDIR"/libigdfcl.so* "$LIBDIR"/libiga64.so* "$LIBDIR"/libopencl-clang2.so*; do
    cp -aP "$f" "$BACKUP"/ 2>/dev/null || true
done
echo "Backed up existing IGC to $BACKUP"

# Copy the four real libs (libigc, libigdfcl, libiga64, libopencl-clang2). libopencl-clang2 is
# easy to forget and its absence shows up only later as CL_OUT_OF_HOST_MEMORY at the text encoder.
installed=0
for pat in 'libigc.so.*' 'libigdfcl.so.*' 'libiga64.so.*' 'libopencl-clang2.so.*'; do
    for f in "$SRC"/$pat; do
        [ -f "$f" ] || continue
        install -m 0755 "$f" "$LIBDIR"/"$(basename "$f")"
        installed=$((installed+1))
    done
done
[ "$installed" -ge 4 ] || { echo "ERROR: only installed $installed libs (need >=4)." >&2; exit 1; }

# Repoint the SONAME symlinks (libX.so / libX.so.2) at the freshly installed 2.35.5 files.
for base in libigc libigdfcl libiga64; do
    real="$(cd "$LIBDIR" && ls -1 ${base}.so.${IGC_VER}* 2>/dev/null | head -1)"
    [ -n "$real" ] || continue
    ln -sf "$real" "$LIBDIR/${base}.so"
    ln -sf "$real" "$LIBDIR/${base}.so.2"
done

command -v ldconfig >/dev/null && ldconfig || true
echo "OK: IGC ${IGC_VER} installed ($installed libs). Restart the GPU service:"
echo "  sudo systemctl restart posterchanai.service"
