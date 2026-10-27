"""Our own relay lists -- the client's and the server's DEFAULT_RELAYS -- name no relay measured dead.

2026-10-26: the post-deploy sign-in check failed on a console error -- a tunnel to wss://relay.0xchat.com, whose name
no longer resolves, opened from the client's own DISCOVERY_RELAYS. purplepag.es (HTTP 502) and relay.nostr.band (no
answer) were dead at that reading and the previous one. Each costs every lookup a doomed connection and an error in
the console. A host listed here comes back only after it is measured answering again.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEAD = {"relay.0xchat.com": "name does not resolve", "purplepag.es": "HTTP 502", "relay.nostr.band": "no answer",
        # 2026-10-27, three attempts each, with and without a User-Agent:
        "relay.froth.zone": "HTTP 503", "nostr.corebreach.com": "TLS handshake fails",
        "relay.sovrgn.co.za": "name does not resolve"}


def test_the_built_in_relay_lists_skip_dead_hosts():
    app = (ROOT / "static/js/client/app.js").read_text()
    for name in ("const DISCOVERY_RELAYS = [", "const PUBLIC_FALLBACK_RELAYS = ["):
        start = app.index(name)
        listed = app[start:app.index("];", start)]
        for host, why in DEAD.items():
            assert "wss://" + host not in listed, (name, host, why)


def test_the_server_default_relays_skip_dead_hosts():
    """The fallback for every blank relay list (the relay's upstream sync, the bots): a dead one costs a connect
    timeout on every firehose reconnect and every outbox publish, for nothing."""
    from app.services.nostr.nostr_service import DEFAULT_RELAYS
    for url in DEFAULT_RELAYS:
        for host, why in DEAD.items():
            assert "://" + host + "/" not in url + "/" and not url.rstrip("/").endswith(host), (url, host, why)
    assert len(DEFAULT_RELAYS) >= 10, "the default set shrank too far to federate"
