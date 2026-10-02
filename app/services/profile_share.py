"""The readable public address of a person -- `poster.place/@<name>` -- and its link preview.

"any way for local users to resolve their profile page like https://poster.place/verita84 like the
fediverse does it?" A bare `/<name>` would share one namespace with every page this app serves
(`/admin`, `/login`, `/search`, `/r`, `/git`, …): a person named like one of them could never be
reached, and every future route could silently take somebody's link. `/@name` is the fediverse's own
form and nothing here starts with `@`, so it cannot collide with anything, now or later.

The name resolves exactly as a repo owner does (git_share.resolve_owner): an npub, a hex key, or a
NIP-05 name THIS node granted -- bare or `name@<this domain>`. A profile's own nip05 claim is never
consulted: anyone can write `nip05: alice@poster.place` into their kind 0.

The card is read off this node's relay and is best-effort: no profile, no relay, a name that resolves
to nobody -- the SPA is still served and says so itself. A crawler failure is never a human failure.
"""
import json
import logging

logger = logging.getLogger(__name__)


async def profile_card(port: int, pubkey_hex: str) -> dict | None:
    """The account's kind 0 on this node's relay, parsed -> {name, about, picture}, or None."""
    from app.services.fedi_bridge_identity import query_one
    ok, ev = await query_one(port, {"kinds": [0], "authors": [pubkey_hex], "limit": 1})
    if not ok or not ev:
        return None
    try:
        prof = json.loads(ev.get("content") or "{}")
    except ValueError:
        return None
    if not isinstance(prof, dict):
        return None
    pic = prof.get("picture") if isinstance(prof.get("picture"), str) else ""
    return {
        "name": str(prof.get("display_name") or prof.get("name") or "").strip(),
        "about": str(prof.get("about") or "").strip(),
        # https only: this lands in <head> on our origin and in every crawler that reads it.
        "picture": pic if pic.startswith("https://") and len(pic) < 2048 else "",
    }


def og_meta(card: dict, handle: str, url: str, fallback_image: str = "") -> dict:
    """A profile card -> the `meta` dict the client shell renders into <head>."""
    shown = card.get("name") or handle
    title = "%s (@%s)" % (shown, handle) if shown != handle else "@" + handle
    desc = card.get("about") or "A profile on Nostr — follow, message and read their posts."
    return {
        "title": title[:120],
        "description": " ".join(desc.split())[:300],
        "url": url,
        "image": card.get("picture") or fallback_image or "",
        "type": "profile",
    }
