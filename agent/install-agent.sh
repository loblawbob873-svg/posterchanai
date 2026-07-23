#!/bin/bash
# Standalone installer for the PosterChan node agent (systemd). Run ON the worker machine.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
RELAY="wss://poster.place/relay"; TRUST=""; CLAUDE=""; DANGER=""
while [ $# -gt 0 ]; do case "$1" in
  --relay) RELAY="$2"; shift 2;;
  --trust) TRUST="$TRUST $2"; shift 2;;
  --claude) CLAUDE=1; shift;;
  --claude-dangerous) CLAUDE=1; DANGER=1; shift;;
  *) echo "unknown arg: $1"; exit 1;;
esac; done
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
[ -n "$CLAUDE" ]  && sudo sed -i '/Environment=PCNODE_DATA/a Environment=PCNODE_CLAUDE=1' "$UNIT"
[ -n "$DANGER" ]  && sudo sed -i '/Environment=PCNODE_DATA/a Environment=PCNODE_CLAUDE_DANGEROUS=1' "$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable --now pcnode-agent.service

echo ""
echo "  ✅ pcnode-agent running. This worker's npub:"
echo ""
echo "     $NPUB"
echo ""
echo "  → Add it in the controller's Admin → Services (Worker nodes: 'name $NPUB')"
echo "    and make sure the controller trusts you back. Logs: journalctl -u pcnode-agent -f"
