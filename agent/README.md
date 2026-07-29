# PosterChan node agent (standalone Nostr worker)

Lightweight worker for machines that can't run the full PosterChanAI app (router.lan, a VPS, a laptop).
It listens on a relay for **encrypted, signed commands from trusted controller npubs only**, runs them
**one at a time**, and returns encrypted results. Protocol: `../docs/NODE_AGENT_NOSTR.md`.

## Install (systemd)
Copy this `agent/` folder to the machine, then:

    ./install-agent.sh --relay wss://poster.place/relay --trust npub1yourcontroller…

It creates a venv, installs deps, generates a keypair, **prints this worker's npub**, and installs +
starts the `pcnode-agent` systemd service. Paste the printed npub into the controller's
Admin → Nodes → *Worker nodes* (name → npub) and its trusted list.

## Manual run
    pip install -r requirements.txt
    python3 pcnode_agent.py --relay wss://poster.place/relay --trust npub1…

`--print-npub` prints the worker npub and exits. Key is stored in `~/.pcnode-agent/agent.key` (0600).

## ⚠️ Security
Commands run as the service user with real shell access. Only add controllers you fully trust to
`--trust`.
