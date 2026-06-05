#!/bin/bash
git commit -a -m fix || true
git push

# Wait for any active GPU inference to finish before restarting.
# Uses flock -n to test the same lock file the service uses.
_wait_gpu_free() {
    local label=$1
    local lockfile=$2
    if ! flock -n "$lockfile" true 2>/dev/null; then
        echo "[$label] GPU busy, waiting..."
        flock "$lockfile" true 2>/dev/null
        echo "[$label] GPU free, restarting"
    fi
}

_wait_gpu_free "arc" /tmp/posterchanai_locks/gpu.lock
sudo systemctl restart posterchanai.service posterchanai-xpu-image.service

# Legacy bot manager. The bots are being migrated INTO posterchanai (botframework/ +
# Admin → Bots), but until the in-app manager's master switch (bots_manager_enabled) is
# turned on for this node, posterchan.service still owns the bots — keep restarting it so
# rollback stays clean. Once cut over: stop+disable posterchan.service, then flip the switch.
if systemctl list-unit-files posterchan.service >/dev/null 2>&1; then
    sudo systemctl restart posterchan.service
fi

ssh nas.lan "
_wait_gpu_free() {
    local label=\$1
    local lockfile=\$2
    if ! flock -n \"\$lockfile\" true 2>/dev/null; then
        echo \"[\$label] GPU busy, waiting...\"
        flock \"\$lockfile\" true 2>/dev/null
        echo \"[\$label] GPU free, restarting\"
    fi
}
cd ~/posterchanai
git fetch origin
git reset --hard origin/master
_wait_gpu_free nas /tmp/posterchanai_locks/gpu.lock
sudo systemctl restart posterchanai
cd ~/posterchan
git fetch origin
git reset --hard origin/master
sudo systemctl restart posterchan
"
