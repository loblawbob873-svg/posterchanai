#!/usr/bin/env bash
# Install/refresh the split systemd units: app + relay + worker + media + bots.
#
#   scripts/install_services.sh [--launcher run-intel.sh] [--dry-run] [--revert]
#
# WHY: with everything under one unit the web app supervises the relay, mediamtx, pion-turn, tor and
# nine bots — so restarting to ship a one-line router change drops every connected Nostr client, kills
# live streams MID-BROADCAST, drops active calls and restarts the bots (which is where their
# startup-race crashes cluster). Splitting them lets sync.sh restart only what a deploy touched.
#
# THE CUTOVER IS ATOMIC-ISH AND MUST BE BOTH HALVES. The app unit is switched to `--role app` (so it
# stops supervising those components) at the same time as the four new units are installed. Doing only
# one half gives you either nothing running them, or two of everything. Two-of-everything fails LOUDLY
# (both bind the same ports) rather than corrupting anything, but neither half alone is a state to
# leave a node in — hence one script that does both.
#
# --revert puts the node back to the single-unit layout: stops+disables the four, drops `--role app`
# from the main unit. That is the rollback, and it needs no code change because the role defaults to
# `all`, which IS the old behaviour.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
LAUNCHER="run-intel.sh"
DRY=0
REVERT=0
UNITS=(relay worker media bots)

while [ $# -gt 0 ]; do
    case "$1" in
        --launcher) LAUNCHER="$2"; shift 2 ;;
        --dry-run)  DRY=1; shift ;;
        --revert)   REVERT=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

[ -x "$REPO/$LAUNCHER" ] || { echo "launcher $REPO/$LAUNCHER not found/executable" >&2; exit 1; }

run() { if [ "$DRY" = 1 ]; then echo "  + $*"; else sudo "$@"; fi; }

MAIN_UNIT=/etc/systemd/system/posterchanai.service

if [ "$REVERT" = 1 ]; then
    echo "[services] reverting to the single-unit layout"
    for r in "${UNITS[@]}"; do
        run systemctl disable --now "posterchanai-$r.service" 2>/dev/null || true
    done
    # Drop --role app so the app supervises everything again (role defaults to 'all').
    if [ "$DRY" = 1 ]; then echo "  + sed -i 's/ --role app//' $MAIN_UNIT"; else
        sudo sed -i 's/ --role app//' "$MAIN_UNIT"
        sudo systemctl daemon-reload
        sudo systemctl restart posterchanai.service
    fi
    echo "[services] reverted — posterchanai.service owns everything again"
    exit 0
fi

echo "[services] repo=$REPO user=$USER_NAME launcher=$LAUNCHER"

for r in "${UNITS[@]}"; do
    case "$r" in
        relay)  desc="Nostr relay";                              after="postgresql.service" ;;
        worker) desc="background worker (pollers/schedulers)";    after="posterchanai-relay.service" ;;
        media)  desc="streaming + TURN (mediamtx, pion-turn)";    after="network.target" ;;
        bots)   desc="bot manager";                               after="posterchanai-relay.service" ;;
    esac
    unit="/etc/systemd/system/posterchanai-$r.service"
    echo "[services] writing $unit"
    body="[Unit]
Description=Posterchan AI — $desc
After=network.target $after
# Ordering only — systemd does not wait for READINESS. The processes keep their own
# _wait_for_relay() loops, which are the actual gate.

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$REPO
Environment=\"PATH=/usr/local/bin:/usr/bin:/bin\"
Environment=\"HOME=/home/$USER_NAME\"
ExecStart=$REPO/$LAUNCHER --role $r

TimeoutStopSec=10
KillMode=mixed
KillSignal=SIGTERM
SendSIGKILL=yes

Restart=always
RestartSec=3
TimeoutStartSec=120
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"
    if [ "$DRY" = 1 ]; then printf '%s\n' "$body" | sed 's/^/    /'; else
        printf '%s\n' "$body" | sudo tee "$unit" >/dev/null
    fi
done

# Switch the app unit to --role app, idempotently.
# sudo: /etc/systemd/system is not world-readable on every distro, and a plain grep here
# returns "Permission denied" — which the verification below correctly treats as "the edit
# did not land" and aborts, leaving the units written but not enabled.
if sudo grep -q -- "--role app" "$MAIN_UNIT" 2>/dev/null; then
    echo "[services] posterchanai.service already runs --role app"
else
    echo "[services] switching posterchanai.service to --role app"
    if [ "$DRY" = 1 ]; then
        echo "  + sed -i 's#^ExecStart=.*\$#&  --role app#' $MAIN_UNIT"
    else
        sudo sed -i "s#^\(ExecStart=.*$LAUNCHER\)\s*\$#\1 --role app#" "$MAIN_UNIT"
        sudo grep -q -- "--role app" "$MAIN_UNIT" || {
            echo "ERROR: could not add --role app to $MAIN_UNIT — REFUSING to continue," >&2
            echo "       because installing the new units without it double-runs everything." >&2
            exit 1
        }
    fi
fi

run systemctl daemon-reload
# Restart the app FIRST: as role 'app' it stops supervising the components, and its children die with
# it. Then bring the new units up. Doing it the other way round means two relays for a moment.
run systemctl restart posterchanai.service
for r in "${UNITS[@]}"; do
    run systemctl enable --now "posterchanai-$r.service"
done

echo "[services] done. Status:"
if [ "$DRY" != 1 ]; then
    systemctl --no-pager --plain -o short status posterchanai.service | head -3 || true
    for r in "${UNITS[@]}"; do
        printf '  %-28s %s\n' "posterchanai-$r" "$(systemctl is-active "posterchanai-$r.service" 2>/dev/null)"
    done
fi
