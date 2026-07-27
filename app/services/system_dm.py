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
import asyncio
import logging

logger = logging.getLogger(__name__)

_profile_done = False   # per-process: publish the notifier's kind-0 once, not on every alert
_profile_tries = 0      # bounded retries — the WoT add is async, so the first attempt can lose the race
_PROFILE_MAX_TRIES = 4


def _sender_for(recipient_hex: str):
    """(seckey, is_fallback) — who this notification comes from.

    PREFER THE OPERATOR KEY: it is the node's own identity, it already has a profile and a history, and
    on a fresh install it is the only identity there is — a brand-new npub DMing you out of nowhere is
    worse than no notification. It fails in exactly one case: when the operator key IS the recipient's
    key (single-admin nodes, where the admin set the instance up with their own key). A DM from you to
    you is a self-DM the client files under note-to-self — no unread count, no toast — so THAT is when
    we fall back to the dedicated notifier identity, and only then."""
    from app.services import keystore
    from app.services.nostr import bip340, nostr_service
    nsec = keystore.get_operator_nsec()
    if nsec:
        try:
            sk = nostr_service.decode_seckey(nsec)
            if sk and bip340.pubkey_from_seckey(sk).hex() != recipient_hex:
                return sk, False
        except Exception:
            pass
    return keystore.get_notifier_seckey(), True


def notifier_npub() -> str:
    """The npub of the fallback identity (used when the operator key is the recipient's own)."""
    from app.services import keystore
    from app.services.nostr import bip340, nostr_service
    sk = keystore.get_notifier_seckey()
    return nostr_service.npub_of(bip340.pubkey_from_seckey(sk).hex())


async def _ensure_profile(sk: bytes, pk_hex: str, port: int) -> None:
    """Publish the notifier's kind-0 so it reads as a name, not a raw npub. Best effort, but RETRIED.

    trigger_wot_add hands the relay a control FILE it picks up on its poll loop, so publishing the
    profile immediately after loses the race — "blocked: not in web of trust" — and the first version
    of this gave up permanently on that, leaving the user with notifications from an unknown npub. So:
    give the add a moment, and if it still fails, try again on the next notification (bounded)."""
    global _profile_done, _profile_tries
    if _profile_done or _profile_tries >= _PROFILE_MAX_TRIES:
        return
    _profile_tries += 1   # count the attempt up front — a raising path must not retry forever either
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
        await asyncio.sleep(3.0)        # let the relay's control-file poll admit the key first

        site = (settings_store.get("site_name", "") or "PosterChan").strip()
        meta = {"name": site, "display_name": f"{site} 🤖",
                "about": f"System notifications from {site} — agent runs, uptime alerts. Replies aren't read."}
        ok, err = await publish_event(port, _event.build_event(sk, 0, json.dumps(meta), tags=[]))
        if ok:
            _profile_done = True
            logger.info("[system-dm] notifier profile published as %s", site)
        else:
            logger.info("[system-dm] notifier profile not published (try %d/%d): %s",
                        _profile_tries, _PROFILE_MAX_TRIES, err)
    except Exception as e:
        logger.debug("[system-dm] notifier profile publish failed: %s", e)


async def send(recipient: str, text: str) -> bool:
    """DM `recipient` (npub or hex) from this node's system identity. True if the relay took it.

    Publishes to the LOCAL relay only — it federates outward from there, the rule every publisher in
    this codebase follows. Never raises: a failed notification must not break its caller."""
    if not recipient or not text:
        return False
    try:
        from app.services import settings_store
        from app.services.nostr import bip340, nip17, nostr_service
        from app.services.nostr_store import publish_event

        hexpk = nostr_service.to_pubkey_hex(recipient)
        if not hexpk:
            logger.info("[system-dm] no usable pubkey for %s — not sent", recipient[:16])
            return False
        sk, fallback = _sender_for(hexpk)
        pk_hex = bip340.pubkey_from_seckey(sk).hex()
        port = settings_store.get_int("nostr_relay_port", 3052)
        # Only the FALLBACK identity gets a profile published for it. The operator key's kind-0 is the
        # node's real identity, managed elsewhere — overwriting it with "system notifications" metadata
        # would clobber the operator's own name and avatar.
        if fallback:
            await _ensure_profile(sk, pk_hex, port)
        ok, err = await publish_event(port, nip17.wrap(sk, hexpk, text))
        if not ok:
            logger.warning("[system-dm] DM to %s not published: %s", recipient[:16], err)
        return bool(ok)
    except Exception as e:
        logger.warning("[system-dm] DM to %s failed: %s", recipient[:16], e)
        return False
