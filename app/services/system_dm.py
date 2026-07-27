"""Server → user notifications, delivered as NIP-17 DMs from the node's OPERATOR key.

The operator key is the npub that installed/set this node up — the one identity every install has, with
no bot required and no extra key minted. On a single-admin node it is usually the ADMIN'S OWN key, so
the notification is a **self-DM**: a note to self, which is exactly the intent — the server telling you
something as you.

That only works because the CLIENT counts notes-to-self as unread (`ingestWrap` in static/js/client/
app.js). It deliberately does not badge the copy of a message this session composed — `sendDm` ingests
its own `toSelf` wrap up front, so the relay's echo is deduped before it can count — which leaves
exactly the arriving ones: system notifications, and your own notes from another device.
"""
import logging

logger = logging.getLogger(__name__)


async def send(recipient: str, text: str) -> bool:
    """DM `recipient` (npub or hex) from this node's operator key. True if the relay took it.

    Publishes to the LOCAL relay only — it federates outward from there, the rule every publisher in
    this codebase follows. Never raises: a failed notification must not break its caller."""
    if not recipient or not text:
        return False
    try:
        from app.services import keystore, settings_store
        from app.services.nostr import nip17, nostr_service
        from app.services.nostr_store import publish_event

        hexpk = nostr_service.to_pubkey_hex(recipient)
        if not hexpk:
            logger.info("[system-dm] no usable pubkey for %s — not sent", recipient[:16])
            return False
        nsec = keystore.get_operator_nsec()
        if not nsec:
            logger.info("[system-dm] no operator key yet — notification not sent")
            return False
        sk = nostr_service.decode_seckey(nsec)
        if not sk:
            return False
        port = settings_store.get_int("nostr_relay_port", 3052)
        ok, err = await publish_event(port, nip17.wrap(sk, hexpk, text))
        if not ok:
            logger.warning("[system-dm] DM to %s not published: %s", recipient[:16], err)
        return bool(ok)
    except Exception as e:
        logger.warning("[system-dm] DM to %s failed: %s", recipient[:16], e)
        return False
