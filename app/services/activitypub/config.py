"""The ActivityPub settings, read one way everywhere (Admin → Nostr → Fediverse (ActivityPub)).

Every key is declared in `SettingsResponse`, so the admin form hydrates it. Instance blocking is NOT
a setting here -- it is the relay's (see blocked_domains). Blank-safe on purpose:
`settings_store.get_bool` reads "" as False, which is right for `activitypub_enabled` (off until an
admin turns it on) and is why the domain falls back to the NIP-05 domain rather than to nothing.
"""
from __future__ import annotations

from app.services import settings_store

AP_CONTENT_TYPE = "application/activity+json"
LD_CONTENT_TYPE = 'application/ld+json; profile="https://www.w3.org/ns/activitystreams"'
PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


def enabled() -> bool:
    return settings_store.get_bool("activitypub_enabled", False)


def domain() -> str:
    """The host actors live on. Must be where `/.well-known/webfinger` answers -- by default the same
    host that serves `/.well-known/nostr.json`, so `@name@host` and `name@host` are one identity."""
    raw = (settings_store.get("activitypub_domain", "") or "").strip()
    if not raw:
        raw = (settings_store.get("nostr_relay_nip05_domain", "") or "").strip()
    raw = raw.lstrip("@").strip().lower()
    for pre in ("https://", "http://"):
        if raw.startswith(pre):
            raw = raw[len(pre):]
    return raw.split("/")[0]


def base_url() -> str:
    d = domain()
    return f"https://{d}" if d else ""


def blocked_domains() -> set:
    """THE RELAY'S BLOCKLIST, READ -- never a second one. Moderation lives on the Nostr relay: every
    event this module stores carries a NIP-48 `proxy` tag naming its original instance, and the
    relay drops events whose proxy host is on `nostr_relay_blocked_relays` (bridges.py). Reading
    the same list here only saves the work of fetching from, and delivering to, an instance the
    relay would refuse anyway. The Pleroma bridge's list is the same kind of decision, so it
    applies too; there is deliberately no ActivityPub-only list to drift from both."""
    from app.services.nostr_relay.bridges import relay_domain
    out = set()
    for key in ("nostr_relay_blocked_relays", "fedi_bridge_blocked_domains"):
        raw = settings_store.get(key, "") or ""
        for tok in raw.replace(",", "\n").split():
            h = relay_domain(tok.lstrip("@"))
            if h:
                out.add(h)
    return out


def host_blocked(host: str) -> bool:
    from app.services.nostr_relay.bridges import _match
    h = (host or "").lower()
    if not h:
        return True
    return _match(h, blocked_domains())


def is_own_host(host: str) -> bool:
    d = domain()
    return bool(d) and (host or "").lower().split(":")[0] == d


def broadcast() -> bool:
    """Whether incoming fediverse content may leave this relay (the Pleroma bridge's switch)."""
    return settings_store.get_bool("fedi_bridge_broadcast", False)
