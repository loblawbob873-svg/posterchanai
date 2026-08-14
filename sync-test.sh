#!/bin/bash
# Branch-aware deploy for the TESTING branch.
# Mirrors sync.sh, but pushes/resets the `testing` branch instead of master so we
# can validate changes on the live LB nodes without disturbing production master.
# (Plain sync.sh hardcodes origin/master on nas, which would ignore this branch.)
BRANCH=testing

git commit -a -m fix || true
# Abort before touching any node if the push fails - otherwise nas would reset to a
# stale origin/testing while the local node runs newer working-tree code (split-brain).
git push -u origin "$BRANCH" || { echo "push failed; aborting deploy" >&2; exit 1; }

# Wait for any active GPU inference to finish before restarting.
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
sudo systemctl restart posterchanai.service

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
cd ~/posterchanai || exit 1
# Only restart if the branch checkout actually succeeds, so nas never runs stale code.
git fetch origin && git checkout -B $BRANCH origin/$BRANCH || { echo 'nas: branch checkout failed' >&2; exit 1; }
_wait_gpu_free nas /tmp/posterchanai_locks/gpu.lock
sudo systemctl restart posterchanai
"
