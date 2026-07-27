"""Tell a user, by DM, the moment an admin grants them AI or Blossom access.

Access is request-then-approve: someone asks, an admin ticks a box later, and until now nothing told
them it had happened. They'd find out by trying again — which is a poor experience for the one
interaction where the answer is "yes".

Sent as a NIP-17 gift-wrapped DM from the instance's OPERATOR key, so it arrives in the same inbox as
any other DM and works in any Nostr client, not just this one. Best-effort by construction: a grant
must never fail because a DM couldn't be wrapped or the relay hiccuped, so every path here swallows
its errors and returns a bool the caller is free to ignore.
"""
import logging

logger = logging.getLogger(__name__)

_MSG = {
    "ai": ("🤖 You've been granted AI access on {site}.\n\n"
           "You can now chat with the assistant, generate images, and use the media commands. "
           "Open the AI tab to start."),
    "stream": ("🔴 You've been granted live-streaming access on {site}.\n\n"
               "Tap Go Live in the sidebar (or ☰ More on a phone) to stream from your camera, "
               "screen, or OBS."),
    "blossom": ("🌸 You've been granted upload access on {site}.\n\n"
                "Images and files you attach now upload to this server's own Blossom storage "
                "instead of a third-party host."),
}


async def notify_access_granted(db, recipient, kinds) -> bool:
    """DM `recipient` about newly granted access. Returns True if the DM was published.

    `recipient` is a User OR a bare npub/hex — the Blossom whitelist works on pubkeys, and creating a
    User row purely so we had something to read an npub off would be a real side effect (an account
    appearing) in service of a notification.

    `kinds` is one of 'ai'/'blossom' or a list of them: granting both at once sends ONE message, not
    two notifications a second apart.

    Never raises. Silently does nothing without a pubkey, without an operator key, or if the relay
    refuses it.
    """
    try:
        if isinstance(kinds, str):
            kinds = [kinds]
        kinds = [k for k in (kinds or []) if k in _MSG]
        if not kinds:
            return False
        if isinstance(recipient, str):
            key = recipient.strip()
        else:
            key = (getattr(recipient, "nostr_npub", "") or "").strip()
        if not key:
            return False

        from app.services import settings_store, system_dm

        site = (settings_store.get("site_name", "") or "this server").strip()
        text = "\n\n".join(_MSG[k].format(site=site) for k in kinds)

        # system_dm (a distinct sender), NOT the operator key: on a single-admin node the operator key
        # is the admin's own, and a self-DM lands in note-to-self with no unread count or toast — a
        # notification nobody is notified by. It publishes to the LOCAL relay, which federates outward.
        ok = await system_dm.send(key, text)
        if ok:
            logger.info("[access-notify] DMed %s about %s access", key[:16], "+".join(kinds))
        return bool(ok)
    except Exception as e:
        # A failed notification must never break the grant that triggered it.
        logger.warning("[access-notify] DM failed: %s", e)
        return False


def notify_access_granted_blocking(db, recipient, kinds) -> None:
    """asyncio.run wrapper for the SYNCHRONOUS admin routes (mirrors users_store.sync_user_blocking).

    Admin → Users is a sync endpoint running in FastAPI's threadpool, so there's no loop to schedule
    on — and asyncio.run is safe precisely because that thread has none of its own."""
    import asyncio
    try:
        asyncio.run(notify_access_granted(db, recipient, kinds))
    except Exception as e:
        logger.warning("[access-notify] blocking DM failed: %s", e)
