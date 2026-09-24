"""The ActivityPub settings, read one way everywhere (Admin → Nostr → Fediverse (ActivityPub)).

Every key is declared in `SettingsResponse`, so the admin form hydrates it. Instance blocking is NOT
a setting here -- it is the relay's (see blocked_domains). The three switches are ON out of the box
(see _on_unless_off), and the domain falls back to the NIP-05 domain rather than to nothing.
"""
from __future__ import annotations

from app.services import settings_store

AP_CONTENT_TYPE = "application/activity+json"
LD_CONTENT_TYPE = 'application/ld+json; profile="https://www.w3.org/ns/activitystreams"'
PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


def _on_unless_off(key: str) -> bool:
    """ON OUT OF THE BOX. A key that was never saved -- or saved blank by a form older than it --
    reads as ON; only an explicit "false" turns it off. `settings_store.get_bool` would read both as
    False, which for these switches would mean a node that never visited the tab never federates."""
    v = settings_store.get(key, None)
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """The master switch. On by default -- but a node with no public domain (no activitypub_domain and
    no NIP-05 domain) still answers nothing, because there is no address to be reachable at."""
    return _on_unless_off("activitypub_enabled")


def everyone() -> bool:
    """Every Nostr user this relay knows is reachable as `npub1…@<domain>`, not only local users."""
    return _on_unless_off("activitypub_everyone")


def dms() -> bool:
    """Direct messages cross the bridge both ways (NIP-17 ⇄ ActivityPub direct notes)."""
    return _on_unless_off("activitypub_dms")


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


def _lists() -> str:
    return "\n".join(settings_store.get(k, "") or "" for k in ("nostr_relay_blocked_relays", "fedi_bridge_blocked_domains"))


def blocked_domains() -> set:
    """THE BLOCKLISTS THAT ALREADY EXIST, READ -- never a third one. Moderation lives on the relay
    (every event stored here carries a NIP-48 `proxy` tag naming its instance, and the relay drops
    blocked ones) and in the fediverse bridge's own list; both are read here, through the one parser
    the bridge uses too (app/services/fedi_blocklist.py), so an entry means the same thing on both
    paths -- including `user@host` lines, which block one ACCOUNT, not its whole instance."""
    from app.services import fedi_blocklist
    return set(fedi_blocklist.parse(_lists())[0])


def host_blocked(host: str) -> bool:
    from app.services import fedi_blocklist
    if not (host or "").strip():
        return True
    return fedi_blocklist.host_blocked(host, fedi_blocklist.parse(_lists())[0])


def account_blocked(acct: str) -> bool:
    """A single blocked account (`user@host` line), or one on a blocked instance."""
    from app.services import fedi_blocklist
    hosts, accounts = fedi_blocklist.parse(_lists())
    return fedi_blocklist.account_blocked(acct, accounts, hosts)


def _host_of_line(line: str) -> str:
    h = line.split("#")[0].strip().lower()
    for pre in ("https://", "http://"):
        if h.startswith(pre):
            h = h[len(pre):]
    return h.split("/")[0].split(":")[0].lstrip("@").strip(".")


def lan_hosts() -> set:
    """Fediverse servers that may resolve to a PRIVATE address from here.

    Split DNS is ordinary on a self-hosted network: a neighbour served by the same front proxy
    resolves to that proxy's LAN address, so the SSRF guard (never fetch a private address on a
    stranger's say-so) refused every request to it -- the import, key fetches and deliveries alike.
    Only names an ADMIN wrote down are trusted. Never a name that merely resolves nearby --
    `router.lan` does too."""
    out = set()
    raw = settings_store.get("activitypub_lan_hosts", "") or ""
    for line in raw.replace(",", "\n").splitlines():
        h = _host_of_line(line)
        if h and "." in h:
            out.add(h)
    return out


def lan_trusted(host: str) -> bool:
    return (host or "").lower().split(":")[0].strip(".") in lan_hosts()


def is_own_host(host: str) -> bool:
    d = domain()
    return bool(d) and (host or "").lower().split(":")[0] == d


def broadcast() -> bool:
    """Whether incoming fediverse content may leave this relay (Admin → Social → "Share fediverse
    posts with other relays"; the key keeps its old name so an existing choice carries over)."""
    return settings_store.get_bool("fedi_bridge_broadcast", False)
