"""Server → user notifications, delivered as NIP-17 DMs from the node's SYSTEM identity.

WHY THIS EXISTS. Every server→user DM here used to be sent from the OPERATOR key. On a single-admin
deployment that key is very often the ADMIN'S OWN key — and a DM from you to you is a **self-DM**:
`rumor.pubkey === ME.pubkey`, so the client files it in your note-to-self thread as a message you
sent. No unread count, no toast, no OS notification. The alert was published, stored, and perfectly
decryptable, and it told the user nothing. That is how "I didn't get an agent notification" and a
dead-looking Messages badge happened on this node, while DMs from the chess bot (a different key)
notified fine.

So system notifications go out from a dedicated, persistent notifier key instead — a distinct sender
is what makes a DM a notification at all. The key is admitted to the WoT and publishes a kind-0 once,
so it shows up as a named conversation rather than a bare npub.

Gift wraps are gated on the RECIPIENT being a WoT member (nostr_relay/server.py), not the sender, so
delivery works regardless; the WoT add is only so the notifier's PROFILE is accepted.
"""
import logging

logger = logging.getLogger(__name__)

_profile_done = False   # per-process: publish the notifier's kind-0 once, not on every alert


def notifier_npub() -> str:
    """The npub this node's system notifications come from."""
    from app.services import keystore
    from app.services.nostr import bip340, nostr_service
    sk = keystore.get_notifier_seckey()
    return nostr_service.npub_of(bip340.pubkey_from_seckey(sk).hex())


async def _ensure_profile(sk: bytes, pk_hex: str, port: int) -> None:
    """Publish the notifier's kind-0 once so it reads as a name, not a raw npub. Best effort."""
    global _profile_done
    if _profile_done:
        return
    _profile_done = True   # set first: a failure must not retry on every single alert
    try:
        import json
        from app.services import settings_store
        from app.services.nostr import event as _event
        from app.services.nostr_store import publish_event
        from app.services.nostr_relay.thread import trigger_wot_add

        try:
            trigger_wot_add([pk_hex])   # so the relay accepts the profile (wraps don't need this)
        except Exception as e:
            logger.debug("[system-dm] wot-add for the notifier failed: %s", e)

        site = (settings_store.get("site_name", "") or "PosterChan").strip()
        meta = {"name": site, "display_name": f"{site} 🤖",
                "about": f"System notifications from {site} — agent runs, uptime alerts. Replies aren't read."}
        ok, err = await publish_event(port, _event.build_event(sk, 0, json.dumps(meta), tags=[]))
        if not ok:
            logger.info("[system-dm] notifier profile not published: %s", err)
    except Exception as e:
        logger.debug("[system-dm] notifier profile publish failed: %s", e)


async def send(recipient: str, text: str) -> bool:
    """DM `recipient` (npub or hex) from this node's system identity. True if the relay took it.

    Publishes to the LOCAL relay only — it federates outward from there, the rule every publisher in
    this codebase follows. Never raises: a failed notification must not break its caller."""
    if not recipient or not text:
        return False
    try:
        from app.services import keystore, settings_store
        from app.services.nostr import bip340, nip17, nostr_service
        from app.services.nostr_store import publish_event

        hexpk = nostr_service.to_pubkey_hex(recipient)
        if not hexpk:
            logger.info("[system-dm] no usable pubkey for %s — not sent", recipient[:16])
            return False
        sk = keystore.get_notifier_seckey()
        pk_hex = bip340.pubkey_from_seckey(sk).hex()
        port = settings_store.get_int("nostr_relay_port", 3052)
        await _ensure_profile(sk, pk_hex, port)
        ok, err = await publish_event(port, nip17.wrap(sk, hexpk, text))
        if not ok:
            logger.warning("[system-dm] DM to %s not published: %s", recipient[:16], err)
        return bool(ok)
    except Exception as e:
        logger.warning("[system-dm] DM to %s failed: %s", recipient[:16], e)
        return False
