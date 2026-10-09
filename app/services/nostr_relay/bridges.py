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


def _norm_host(host: str) -> str:
    """Canonical host for blocklist matching: lowercase, strip a trailing FQDN dot and any :port.
    Without this `mostr.pub.` (a legal absolute form that resolvers treat identically) and
    `mostr.pub:443` both slipped past a blocked-domain check."""
    h = (host or "").strip().lower()
    if h.startswith("[") and "]" in h:          # [v6]:port
        h = h[1:h.index("]")]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def _match(host: str, domains) -> bool:
    host = _norm_host(host)
    ds = {_norm_host(d) for d in (domains or ())}
    return bool(host) and any(host == d or host.endswith("." + d) for d in ds)


def has_proxy_tag(ev: dict) -> bool:
    """NIP-48: the event is MIRRORED from another protocol (ActivityPub / atproto / …) via a bridge.
    Any `proxy` tag means bridged-from-elsewhere — the only reliable signal for fediverse / Bluesky
    bridge content (mostr.pub, momostr.pink, ditto.pub, brid.gy, …), because the bridge domain
    itself never appears in the event (the nip05 is on a normal domain, the proxy URL points at the
    ORIGINAL instance)."""
    for t in ev.get("tags") or []:
        if len(t) >= 2 and t[0] == "proxy" and t[1]:
            return True
    return False


# THE PROTOCOLS THAT MEAN "THIS ACCOUNT IS A FEDIVERSE OR BLUESKY PERSON MIRRORED IN BY A BRIDGE".
# NIP-48's third element names what the `proxy` tag points INTO. Only these two are social bridges;
# the others seen on this relay are not, and must not be caught by this rule: torrent indexers put the
# source URL there (nyaa / piratebay, 16k events feeding the Torrents view), and rss / web / x.com /
# github are single-purpose feed mirrors.
SOCIAL_BRIDGE_PROTOCOLS = frozenset({"activitypub", "atproto"})


def is_social_mirror(ev: dict) -> bool:
    """The event was signed by a BRIDGE on behalf of a fediverse/Bluesky account (NIP-48 `proxy`
    tag, protocol activitypub|atproto) — i.e. its author is a mirror account, whatever its kind.

    This is the signal the domain blocklist could not see. Classifying by the BRIDGE's domain needs
    the account's kind-0 (`nip05: graf@poa-st.mostr.pub`), and a bridge account's profile mostly
    never reaches this relay — measured 2026-10-06: 53,212 stored mirror events from 4,468 accounts,
    and 18 of their profiles. The `proxy` tag is on every event the bridge signs, and it points at
    the ORIGIN server (poa.st), never at the bridge, which is why the host match never fired.

    Our OWN fediverse puppets carry the same tag; callers must exempt them (gate.is_puppet_event),
    and operators/registered users, exactly as the other bridge rules do."""
    for t in ev.get("tags") or []:
        if (isinstance(t, (list, tuple)) and len(t) >= 3 and t[0] == "proxy" and t[1]
                and str(t[2]).strip().lower() in SOCIAL_BRIDGE_PROTOCOLS):
            return True
    return False


# PUBLIC timeline kinds the "block bridged posts" filter is allowed to touch: notes + reposts only.
# Crucially NOT kind 4 / 1059 (DMs) — a fediverse user DMing through a bridge sends a proxy-tagged
# kind-4, and blocking/purging those silently eats incoming DMs. Also spares reactions/profiles/etc.
_BRIDGEABLE_KINDS = frozenset({1, 6})


def is_bridged_post(ev: dict) -> bool:
    """True for a bridged PUBLIC POST (note/repost with a NIP-48 proxy tag) — what the opt-in
    'block bridged posts' relay setting targets. Scoped to timeline kinds so DMs (kind 4 / NIP-17
    1059), reactions, profiles and relay lists are never blocked or purged by the bridge filter."""
    k = ev.get("kind")
    return (int(k) if k is not None else 1) in _BRIDGEABLE_KINDS and has_proxy_tag(ev)


def author_on_blocked_bridge(ev: dict, domains) -> bool:
    """nostrify-style DomainPolicy signal: the author's OWN profile (kind 0) declares a `nip05`
    whose domain — or a subdomain of it — is blocklisted. This is the DEFINITIVE "this account
    lives on the bridge" test: a mirror account literally identifies as `handle@mostr.pub`, whereas
    a real user's nip05 is on their own domain (never the bridge). It is therefore safe to act on
    even for a *followed* account, unlike the weaker relay-list / proxy hints in
    reveals_blocked_bridge() (a real fedi-crossposter can carry those, so those stay member-exempt)."""
    if not domains:
        return False
    k = ev.get("kind")
    if (int(k) if k is not None else 1) != 0:
        return False
    try:
        nip05 = (json.loads(ev.get("content") or "{}").get("nip05") or "").strip().lower()
    except Exception:
        return False
    return "@" in nip05 and _match(nip05.rsplit("@", 1)[-1], domains)


def reveals_blocked_bridge(ev: dict, domains) -> bool:
    """True if `ev` shows its author is hosted on a blocked bridge domain (so the whole account
    should be denied). Looks at kind-0 nip05 and any `proxy` tag host -- never at relay lists."""
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
    # A RELAY LIST THAT MERELY NAMES A BRIDGE RELAY IS NOT A BRIDGE ACCOUNT (2026-10-09). Plenty of
    # real people list relay.ditto.pub or relay.momostr.pink to READ fediverse content; treating that as
    # "hosted on the bridge" refused their relay list ("blocked: bridged relay not accepted") AND marked
    # the whole account bridged, barring everything else they post -- Vyram, a Concord user, could not
    # save his settings. What does identify a bridge account is unchanged: a nip05 ON the bridge domain
    # (handle@mostr.pub) and the NIP-48 proxy tag a bridge stamps on what it mirrors.
    return False


def _parents(host: str):
    """Every domain `host` is a subdomain of, longest first — `relay.bchnostr.com` →
    ['bchnostr.com', 'com']. Used only to EXPLAIN a blocklist entry, never to widen one."""
    h = _norm_host(host)
    parts = h.split(".")
    return [".".join(parts[i:]) for i in range(1, len(parts))] if len(parts) > 1 else []


def explain_blocklist(entries, nip05_domains):
    """Say what each blocklist entry actually catches, and what it probably meant to.

    AN ENTRY THAT MATCHES NOTHING IS INDISTINGUISHABLE FROM ONE THAT WORKS. The field is labelled
    "blocked bridges/relays", so `relay.bchnostr.com` is exactly what an operator types — and it
    matches nothing, because an account is classified by the domain in its OWN nip05 and every one
    of those reads `handle@bchnostr.com`. Matching is suffix-based downwards (`bchnostr.com` covers
    `relay.bchnostr.com`) and never upwards, so the entry was a subdomain OF the thing meant, and
    the relay carried on serving those posts with nothing anywhere to say so. Reported as "I added
    relay.bchnostr.com under blocked bridges/relays and I still see BCHnostr posts".

    `nip05_domains` is the set of domains stored kind-0 profiles actually identify as. Returns one
    row per entry: `matches` (how many of those it catches) and, when that is zero, `suggestion` —
    the nearest PARENT domain that would catch some, which is the whole point. Nothing here changes
    what is blocked; widening an operator's entry on their behalf could take out `damus.io` because
    they blocked `relay.damus.io`.
    """
    counts = {}
    for d in (nip05_domains or ()):
        h = _norm_host(d)
        if h:
            counts[h] = counts.get(h, 0) + 1
    out = []
    for raw in (entries or ()):
        host = _norm_host(relay_domain(raw) or raw)
        if not host:
            continue
        matches = sum(n for h, n in counts.items() if h == host or h.endswith("." + host))
        suggestion = ""
        if not matches:
            for parent in _parents(host):
                if any(h == parent or h.endswith("." + parent) for h in counts):
                    suggestion = parent
                    break
        out.append({"entry": host, "matches": matches, "suggestion": suggestion})
    return out
