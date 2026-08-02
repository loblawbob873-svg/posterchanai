#!/bin/bash

# ---------------------------------------------------------------------------------------------
# Pre-push lint gate: UNDEFINED NAMES ONLY.
#
# py_compile does NOT catch a NameError, and the services swallow their own exceptions, so a
# missing name ships looking healthy and silently drops work. That is exactly how the fediverse
# bridge lost every post (fedi_normalize called html.unescape with only `import html as _html`)
# and how the social relay lost Nostr notifications -- both invisible to compile checks AND to a
# log grep for Traceback/ImportError.
#
# Deliberately narrow: only "undefined name", so it stays at zero false positives and nobody
# learns to ignore it. Star-import notices are excluded (pyflakes cannot see through them --
# app/services/effects_service/ is NOT covered by this gate). Unused imports are NOT gated;
# they are pre-existing and noisy.
#
# Skip in an emergency with:  SKIP_LINT=1 ./sync.sh
# ---------------------------------------------------------------------------------------------
if [ -z "$SKIP_LINT" ] && [ -x venv-unified/bin/python ] && venv-unified/bin/python -c "import pyflakes" 2>/dev/null; then
    _undef=$(venv-unified/bin/python -m pyflakes app/ botframework/ 2>&1 \
             | grep "undefined name" | grep -v "unable to detect")
    if [ -n "$_undef" ]; then
        echo "[sync] ABORT: undefined name(s) -- this WILL fail at runtime:"
        echo "$_undef" | sed 's/^/    /'
        echo "[sync] fix them, or bypass with: SKIP_LINT=1 ./sync.sh"
        exit 1
    fi
    echo "[sync] lint OK (no undefined names)"
else
    echo "[sync] WARN: pyflakes unavailable -- skipping the undefined-name gate"
fi

# Remember what was deployed BEFORE this commit, so the restart set can be computed from
# the actual diff rather than from guesswork.
_PREV_HEAD="$(git rev-parse HEAD 2>/dev/null || echo HEAD)"
git commit -a -m fix || true
# Deploy to PRODUCTION. `origin` is now the NOSTR repo on the built-in GRASP host
# (nostr://<npub>/relay.poster.place/posterchanai -> https://poster.place/git/<npub>/posterchanai.git);
# Gitea is gone. ngit publishes the signed 30618 that authorizes the push to the repo's relays, one of
# which (wss://poster.place/git) IS the hosting node's relay — the one pre-receive reads.
# Needs git-remote-nostr on PATH; it is installed in /usr/local/bin on every node so this works from
# a non-interactive ssh and from sudo, not just from an interactive login shell.
git push origin master

# Also push to the public `github` mirror (master → main). This TRIGGERS the Android app build:
# GitHub Actions (.github/workflows/android.yml) rebuilds the bundled web UI, compiles + signs the APK,
# and publishes it to the rolling `apk-latest` Release — which poster.place/apk serves. The workflow's
# `paths` filter only rebuilds when the client / mobile project actually changed, so it's a no-op build
# on unrelated deploys. NOTE: this publishes every commit to the PUBLIC mirror on each deploy.
git push github master:main || echo "[sync] WARN: github push (Android APK build trigger) failed"

# NOTE: scripts/grasp_mirror.py is no longer called from here. It existed to copy commits from a
# Gitea `origin` onto the nostr repo; now that `origin` IS the nostr repo, the push above already put
# them there and mirroring would be a circular no-op. The script is kept for manual/recovery use.

# Mirror the freshly-built APK to a local path so poster.place/apk serves the bytes DIRECTLY from this
# server (behind Cloudflare — a nearby CDN edge, with Range/resume), which downloads far more reliably on
# slow/throttled mobile links than bouncing to GitHub's distant CDN. The CI build takes ~2-3 min, so do it
# detached after a delay (best-effort; /apk falls back to the GitHub redirect until the mirror updates).
( sleep 240; /home/verita84/posterchan-apk/refresh.sh ) >/tmp/apk-refresh.log 2>&1 &

# Update router.lan (192.168.0.1) nginx's static checkout. It serves /static from LOCAL files
# (root /srv/posterchanai → a git clone), decoupling asset loading from server1 so the restart below
# can't break the page with "Loading failed for <script>". Pull on EVERY webui change or it serves
# stale JS/CSS. (See memory: project_client_nginx_cache.)
# Also on the nostr repo now. It runs under sudo, which is why git-remote-nostr lives in
# /usr/local/bin rather than ~/.local/bin — sudo's secure_path would not find the latter.
ssh router.lan "cd /srv/posterchanai && sudo git fetch origin && sudo git reset --hard origin/master" \
    || echo "[sync] WARN: router.lan static git pull failed"

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

# Restart only what this deploy actually TOUCHED. With the relay, worker, mediamtx/TURN and the bots
# split into their own units, a blanket `restart posterchanai.service` would hand back the very outage
# the split removed: every connected Nostr client dropped, live streams killed mid-broadcast, active
# calls dropped, nine bots restarted into their startup race — to ship a one-line router change.
#
# scripts/deploy_targets.py maps changed paths -> units and is deliberately conservative: anything it
# does not recognise restarts EVERYTHING, because under-restarting leaves stale code running with no
# error anywhere ("the fix didn't work"), which is far harder to notice than an extra restart.
#
# A node that has NOT been split still has only posterchanai.service; `systemctl restart` on a unit
# that does not exist is skipped below, so this is safe on both layouts.
_restart_units() {
    local units="$1"
    if [ -z "$units" ]; then
        echo "[sync] nothing to restart (no server-side code changed)"
        return
    fi
    for u in $units; do
        if systemctl list-unit-files "$u" >/dev/null 2>&1 && systemctl cat "$u" >/dev/null 2>&1; then
            echo "[sync] restarting $u"
            sudo systemctl restart "$u"
        fi
    done
}

_TARGETS="$(venv-unified/bin/python scripts/deploy_targets.py "$_PREV_HEAD..HEAD" 2>/dev/null \
            || echo posterchanai.service)"
echo "[sync] deploy targets: ${_TARGETS:-<none>}"
_restart_units "$_TARGETS"

# server1 is cut over: the bots now run via the in-app manager (botframework/ + Admin → Bots,
# bots_manager_enabled). The legacy posterchan.service is stopped+disabled here, so do NOT
# restart it — `systemctl restart` would re-activate a disabled unit and double-run the bots.
# Only (re)start it if it's still ENABLED (i.e. a node that hasn't been cut over yet).
if systemctl is-enabled posterchan.service >/dev/null 2>&1; then
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
# Pulls from the nostr repo (origin) over https://poster.place/git — no Gitea. The mirror step that
# used to follow this is gone: the push in the parent script already published these commits to the
# nostr repo, so there is nothing left to mirror.
_NAS_PREV=\$(git rev-parse HEAD 2>/dev/null || echo HEAD)
git fetch origin
git reset --hard origin/master
_wait_gpu_free nas /tmp/posterchanai_locks/gpu.lock
# Same targeted restart as server1: only the units this deploy touched. Computed on nas from its OWN
# pre-pull HEAD, because a node can be behind by more than one commit. Falls back to restarting the
# app if anything about that fails — never silently restart nothing.
_NAS_TARGETS=\$(venv-unified/bin/python scripts/deploy_targets.py \$_NAS_PREV..HEAD 2>/dev/null || echo posterchanai.service)
echo \"[nas] deploy targets: \${_NAS_TARGETS:-<none>}\"
for u in \$_NAS_TARGETS; do
    if systemctl cat \$u >/dev/null 2>&1; then echo \"[nas] restarting \$u\"; sudo systemctl restart \$u; fi
done
# nas is cut over: its bots now run via the in-app manager (botframework/ + Admin → Bots).
# posterchan.service is stopped+disabled here, so do NOT restart it (a 'restart' would
# re-activate a disabled unit and double-run the bots). Only refresh/restart it if it's
# still ENABLED (a node not yet migrated).
if systemctl is-enabled posterchan >/dev/null 2>&1; then
    cd ~/posterchan && git fetch origin && git reset --hard origin/master
    sudo systemctl restart posterchan
fi
"
