"""This node under another name is dialled directly, never through its own Concord tunnel (2026-10-21).

poster.place's relay is advertised as wss://poster.place/relay and also answers as wss://relay.poster.place and
wss://poster.place/git. Tunnelled, those dialled the node from itself and were refused 403 (12 of 12 in production),
so the PosterChan repository's issues never loaded in its Concord room. Runs the SHIPPED cordVia."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_this_nodes_own_relays_skip_the_tunnel_and_others_do_not():
    r = subprocess.run(["node", "tests/client/concord_tunnel_skips_this_node_runtime.mjs"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr
