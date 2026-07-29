#!/bin/bash
# Standalone installer for the PosterChan node agent (systemd). Run ON the worker machine.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
RELAY=""; TRUST=""
while [ $# -gt 0 ]; do case "$1" in
  --relay) RELAY="${RELAY:+$RELAY,}$2"; shift 2;;   # repeatable — comma-joined (one controller relay each)
  --trust) TRUST="$TRUST $2"; shift 2;;
  *) echo "unknown arg: $1"; exit 1;;
esac; done
[ -z "$RELAY" ] && RELAY="wss://poster.place/relay"
# Comma-join (NOT space): systemd Environment= splits on spaces, so a space-separated list would
# become bogus extra assignments. pcnode_agent.py splits PCNODE_RELAY on commas/whitespace.
[ -z "$TRUST" ] && { read -rp "Controller npub to trust: " TRUST; }

echo "[pcnode] creating venv + deps…"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" -q install -r "$DIR/requirements.txt"
PY="$DIR/venv/bin/python"
DATA="$HOME/.pcnode-agent"

echo "[pcnode] generating keypair…"
NPUB="$(PCNODE_DATA="$DATA" "$PY" "$DIR/pcnode_agent.py" --print-npub)"

UNIT=/etc/systemd/system/pcnode-agent.service
echo "[pcnode] installing systemd unit → $UNIT"
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$DIR|g" -e "s|__RELAY__|$RELAY|g" \
    -e "s|__TRUST__|$(echo $TRUST)|g" -e "s|__DATA__|$DATA|g" -e "s|__PY__|$PY|g" \
    "$DIR/pcnode-agent.service" | sudo tee "$UNIT" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable pcnode-agent.service
sudo systemctl restart pcnode-agent.service   # restart (not just enable --now) so a REINSTALL reloads the unit

echo ""
echo "  ✅ pcnode-agent running. This worker's npub:"
echo ""
echo "     $NPUB"
echo ""
echo "  → Add it in the controller's Admin → Nodes (Worker nodes: 'name $NPUB')"
echo "    and make sure the controller trusts you back. Logs: journalctl -u pcnode-agent -f"
