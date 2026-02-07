#!/bin/bash
# NVIDIA kernel module reset
# Unloads and reloads NVIDIA modules in the correct order to recover from
# GPU hangs, memory fragmentation, or driver instability.
# Must be run as root (e.g. sudo ./nvidia_reset.sh).
# Ensure no processes are using the GPU (stop posterchanai, X, etc.) before running.

# Unload in reverse dependency order (dependents first)
UNLOAD_MODULES=(
    nvidia_uvm
    nvidia_uvm_lite
    nvidia_drm
    nvidia_modeset
    nvidia
)

# Reload in dependency order (core first)
LOAD_MODULES=(
    nvidia
    nvidia_modeset
    nvidia_drm
    nvidia_uvm
)

log() { echo "[nvidia_reset] $*"; }
warn() { echo "[nvidia_reset] WARNING: $*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
    warn "This script must be run as root (e.g. sudo $0)"
    exit 1
fi

# Check that nvidia module exists (driver installed)
if ! modinfo nvidia &>/dev/null; then
    warn "nvidia kernel module not found - is the driver installed?"
    exit 1
fi

log "Unloading NVIDIA modules..."
for mod in "${UNLOAD_MODULES[@]}"; do
    if lsmod | grep -q "^${mod} "; then
        log "  rmmod $mod"
        rmmod "$mod" 2>/dev/null || warn "rmmod $mod failed (may be in use)"
    fi
done

# Second pass: try again for any still loaded (e.g. refs dropped)
for mod in "${UNLOAD_MODULES[@]}"; do
    if lsmod | grep -q "^${mod} "; then
        log "  retry rmmod $mod"
        rmmod "$mod" 2>/dev/null || true
    fi
done

log "Waiting 2s for devices to release..."
sleep 2

log "Loading NVIDIA modules..."
for mod in "${LOAD_MODULES[@]}"; do
    if ! lsmod | grep -q "^${mod} "; then
        log "  modprobe $mod"
        modprobe "$mod" || warn "modprobe $mod failed"
    fi
done

log "Done. Verify with: nvidia-smi"
exit 0
