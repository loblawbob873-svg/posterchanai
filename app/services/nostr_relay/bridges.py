"""Bridge / relay blocklist — keep accounts bridged in from blocked relays out of the WoT relay.

Bridges like **mostr.pub** and **brid.gy** mirror fediverse / Bluesky accounts into Nostr. A
bridged account is recognisable not by its notes (a mostr note's NIP-48 `proxy` tag points at the
ORIGINAL source server, e.g. mastodon.social, not at the bridge) but by its **identity**:

  - its profile (kind 0) advertises a `nip05` on the bridge domain — e.g. `alice_at_x@mostr.pub`;
  - its relay list (kind 10002) / contact list (kind 3) points at the bridge relay — `wss://mostr.pub`.

So we classify the *account* from those events and then drop everything it authors. Domain match is
suffix-based, so blocking `mostr.pub` also covers `bsky-bridge.mostr.pub`, and `brid.gy` covers
`bsky.brid.gy`. The `proxy`-tag host is checked too (harmless, and it catches bridges whose proxy
URL does point at their own domain).
"""

import json
from urllib.parse import urlparse


def relay_domain(s: str) -> str:
    """Normalise an admin-entered relay/bridge entry (URL or bare host) to a lowercase host."""
    s = (s or "").strip().lower().rstrip("/")
    if not s:
        return ""
    if "://" not in s:
        s = "//" + s                       # let urlparse treat a bare host as the netloc
    try:
        return urlparse(s).hostname or ""
    except Exception:
        return ""


def _match(host: str, domains) -> bool:
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def reveals_blocked_bridge(ev: dict, domains) -> bool:
    """True if `ev` shows its author is hosted on a blocked bridge domain (so the whole account
    should be denied). Looks at kind-0 nip05, kind-3/10002 relay hints, and any `proxy` tag host."""
    if not domains:
        return False
    tags = ev.get("tags") or []
    for t in tags:                                              # NIP-48 proxy tag host
        if len(t) >= 2 and t[0] == "proxy" and _match(relay_domain(t[1]), domains):
            return True
    k = ev.get("kind")
    kind = int(k) if k is not None else 1
    if kind == 0:                                               # profile nip05 (handle@bridge)
        try:
            nip05 = (json.loads(ev.get("content") or "{}").get("nip05") or "").strip().lower()
        except Exception:
            nip05 = ""
        if "@" in nip05 and _match(nip05.rsplit("@", 1)[-1], domains):
            return True
    if kind in (3, 10002):                                      # relay-list / contact-list relays
        for t in tags:
            if len(t) >= 2 and t[0] == "r" and _match(relay_domain(t[1]), domains):
                return True
    return False
