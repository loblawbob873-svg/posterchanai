"""Identity provisioning for the Nostr ↔ Fediverse bridge.

Turns a fediverse account into a Nostr "puppet": a deterministic keypair (see nostr.bridge_keys),
a NIP-05 name on this instance, and a mirrored kind-0 profile. The relay validates puppet events by
re-deriving the key from the actor URI carried in a `fedibridge` tag, so nothing here has to register
keys with the relay — the app just signs and publishes, and the relay serves the puppet's NIP-05 the
moment it stores the kind-0 (server._register_bridge_nip05).

Public surface:
  - actor_uri_of(account)           canonical AP actor URI (the derivation key)
  - nip05_name_for(acct)            stable local-part, e.g. alice@mastodon.social → alice_mastodon.social
  - puppet_for(account)             {seckey, pubkey_hex, npub, actor_uri, nip05_name, acct, host}
  - ensure_puppet(db, port, account)  provision/refresh registry row + kind-0; returns the puppet dict
  - build_event(p, kind, content, tags, object_uri, broadcast)   sign a puppet event (adds bridge tags)
  - publish(port, ev)               publish to the local relay; (ok, msg)
"""

import re
import json
import time
import hashlib
import logging
from datetime import datetime

from app.services import keystore, settings_store
from app.services.nostr import bridge_keys, nostr_service
from app.services.nostr.event import build_event as _build_event
from app.services.nostr_store import _ws_publish

logger = logging.getLogger(__name__)


def _secret() -> bytes:
    return keystore.get_bridge_secret()


def nip05_domain() -> str:
    """The domain puppet NIP-05 identifiers are served under (must match where this node's
    /.well-known/nostr.json is reachable). Reuses the relay's NIP-05 domain setting."""
    return (settings_store.get("nostr_relay_nip05_domain", "") or "").strip().lstrip("@").lower()


def _sanitize(s: str) -> str:
    """NIP-05 local-part charset is a-z0-9-_. — collapse everything else out."""
    return re.sub(r"[^a-z0-9_.\-]", "", (s or "").strip().lower()).strip("._-")


def actor_uri_of(account: dict) -> str:
    """The canonical ActivityPub actor URI for a Mastodon/Pleroma account object. `url` is the
    profile URL (stable, canonical); `uri` is the AP id on some servers. Prefer whichever is set."""
    return (account.get("uri") or account.get("url") or "").strip()


def acct_of(account: dict, instance_host: str = "") -> str:
    """Fully-qualified handle user@host. Mastodon/Pleroma give bare `acct` for LOCAL users (no host),
    so qualify it with the instance we read it from."""
    acct = (account.get("acct") or account.get("username") or "").strip()
    if acct and "@" not in acct and instance_host:
        acct = f"{acct}@{instance_host}"
    return acct.lstrip("@")


def nip05_name_for(acct: str) -> str:
    """Stable local-part for a handle: alice@mastodon.social → alice_mastodon.social. Unique as long
    as the handle is (it is, host-qualified), so it maps 1:1 to a puppet without a disambiguator."""
    local, _, host = (acct or "").partition("@")
    base = _sanitize(local) or "user"
    h = _sanitize(host)
    return (f"{base}_{h}" if h else base)[:64].strip("._-")


def puppet_for(account: dict, instance_host: str = "") -> dict:
    """Resolve the full puppet identity for a fediverse account (no I/O, no DB)."""
    actor_uri = actor_uri_of(account)
    acct = acct_of(account, instance_host)
    sk = bridge_keys.derive_seckey(_secret(), actor_uri)
    pubkey_hex = nostr_service.derive_pubkey(sk)
    host = acct.partition("@")[2] or instance_host
    return {
        "seckey": sk,
        "pubkey_hex": pubkey_hex,
        "npub": nostr_service.npub_of(pubkey_hex),
        "actor_uri": actor_uri,
        "acct": acct,
        "host": host,
        "nip05_name": nip05_name_for(acct),
        "display_name": (account.get("display_name") or account.get("name") or "").strip(),
        "avatar_url": (account.get("avatar") or account.get("avatar_static")
                       or account.get("avatarUrl") or "").strip(),
        "about": (account.get("note") or account.get("description") or "").strip(),
    }


def _profile_content(p: dict) -> dict:
    domain = nip05_domain()
    out = {
        "name": p["display_name"] or p["nip05_name"],
        "display_name": p["display_name"] or p["acct"].partition("@")[0],
        "about": ((p["about"] + "\n\n") if p["about"] else "") + f"🔗 bridged from {p['acct']} (fediverse)",
        "fediverse": p["acct"],
        "bridged": True,
    }
    if p["avatar_url"]:
        out["picture"] = p["avatar_url"]
    if domain:
        out["nip05"] = f"{p['nip05_name']}@{domain}"
    return out


def _profile_sig(p: dict) -> str:
    raw = "\x1f".join([p["display_name"], p["avatar_url"], p["about"][:200], nip05_domain()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_event(p: dict, kind: int, content: str, tags: list | None = None,
                object_uri: str | None = None, broadcast: bool = False) -> dict:
    """Sign a puppet event, attaching the mandatory `fedibridge` actor anchor (so the relay validates
    it), a NIP-48 `proxy` deep-link to the original fedi object, and — unless broadcast is enabled —
    a `nofederate` marker so the relay keeps the mirror local-only (see server._broadcastable)."""
    t = list(tags or [])
    t.append([bridge_keys.ACTOR_TAG, p["actor_uri"]])
    if object_uri:
        t.append(["proxy", object_uri, "activitypub"])
    if not broadcast:
        t.append(["nofederate"])
    return _build_event(p["seckey"], kind, content, tags=t)


async def publish(port: int, ev: dict) -> tuple[bool, str]:
    return await _ws_publish(port, ev)


async def ensure_puppet(db, port: int, account: dict, instance_host: str = "") -> dict | None:
    """Provision (or refresh) a fediverse account's puppet: upsert the registry row, and (re)publish
    its kind-0 profile when first seen or when the display name/avatar/bio/domain changed. Returns
    the puppet dict, or None if the account has no usable actor URI."""
    from app.models import FediPuppet
    p = puppet_for(account, instance_host)
    if not p["actor_uri"]:
        return None

    row = db.query(FediPuppet).filter(FediPuppet.actor_uri == p["actor_uri"]).first()
    now = datetime.utcnow()
    sig = _profile_sig(p)
    need_profile = False
    if row is None:
        row = FediPuppet(actor_uri=p["actor_uri"], acct=p["acct"], instance_host=p["host"],
                         pubkey_hex=p["pubkey_hex"], nip05_name=p["nip05_name"],
                         display_name=p["display_name"], avatar_url=p["avatar_url"],
                         profile_sig=None, last_seen=now, created_at=now)
        db.add(row)
        need_profile = True
    else:
        row.acct = p["acct"]
        row.instance_host = p["host"]
        row.display_name = p["display_name"]
        row.avatar_url = p["avatar_url"]
        row.nip05_name = p["nip05_name"]
        row.last_seen = now
        # Re-publish the kind-0 only when the display name / avatar / bio / domain actually changed
        # (profile_sig captures all of those) or it was never published.
        need_profile = row.profile_sig != sig
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.debug("[fedi-bridge] puppet upsert failed for %s: %s", p["acct"], e)

    if need_profile:
        broadcast = str(settings_store.get("fedi_bridge_broadcast", "false")).lower() in ("1", "true", "yes", "on")
        ev = build_event(p, 0, json.dumps(_profile_content(p)), object_uri=p["actor_uri"],
                         broadcast=broadcast)
        ok, msg = await publish(port, ev)
        if ok:
            row.profile_sig = sig
            try:
                db.commit()
            except Exception:
                db.rollback()
        else:
            logger.debug("[fedi-bridge] profile publish failed for %s: %s", p["acct"], msg)
    return p
