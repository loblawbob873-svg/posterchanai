"""PosterChan's NIP-89 APPLICATION HANDLER (kind 31990), signed by this node's operator key.

Every event the client publishes carries `["client", "PosterChan", "31990:<operator>:posterchan", "<relay>"]`. The name
is what "via PosterChan" shows and what app dashboards (radarnostr, nostr.band) count; the address is what lets a
client turn that label into the app's name, picture and links -- without an announcement behind it the address resolves
to nothing and the label is plain text, or is dropped. So the node publishes the announcement it points to: its name,
picture and website, the kinds it handles, and `web` handlers that open a NIP-19 entity on this instance
(`<origin>/<bech32>`, the same address the client shell already routes).

Published on this node's relay (which forwards it upstream), and only when it CHANGED -- a content hash is kept, so a
restart does not rewrite it. No public origin or no operator key: nothing is published, and `client_tag()` falls back to
the plain name, which is still a valid NIP-89 tag.
"""
from __future__ import annotations

import hashlib
import json
import logging

from app.services import emoji_sets, settings_store
from app.services.activitypub import config as ap_config
from app.services.nostr import event as nostr_event

logger = logging.getLogger(__name__)

KIND_HANDLER = 31990
D_TAG = "posterchan"
APP_NAME = "PosterChan"
STATE_KEY = "app_handler_cursor"   # {"hash": ...}; per node (a `_cursor` key never leaves it)

# What the client publishes and can open (NIP-89 `k` tags).
HANDLED_KINDS = (0, 1, 3, 5, 6, 7, 16, 1063, 1068, 1111, 1311, 9735, 9802, 10000, 10002, 10063, 30023, 30030, 30311,
                 30617, 34550)
WEB_ENTITIES = ("npub", "nprofile", "note", "nevent", "naddr")


def address() -> str:
    """`31990:<operator hex>:posterchan`, or '' with no operator key."""
    _sk, pk = emoji_sets._operator()
    return f"{KIND_HANDLER}:{pk}:{D_TAG}" if pk else ""


def client_tag() -> list:
    """The tag the client stamps on what it publishes."""
    a, relay = address(), emoji_sets._relay_url()
    if a and ap_config.base_url():
        return ["client", APP_NAME, a] + ([relay] if relay else [])
    return ["client", APP_NAME]


def build_tags_and_content(base: str) -> tuple[list, str]:
    base = base.rstrip("/")
    content = json.dumps({
        "name": APP_NAME, "display_name": APP_NAME,
        "picture": base + "/static/icon-512.png", "banner": base + "/static/screenshot-wide.png",
        "about": "A Nostr client and relay with AI chat, communities (Concord), encrypted files, mail, "
                 "calendar, notes, music and live streaming.",
        "website": base,
    }, separators=(",", ":"))
    tags = [["d", D_TAG], ["alt", "Nostr app: " + APP_NAME]]
    tags += [["k", str(k)] for k in HANDLED_KINDS]
    tags += [["web", base + "/<bech32>", e] for e in WEB_ENTITIES]
    return tags, content


async def publish(force: bool = False) -> dict:
    base = ap_config.base_url()
    sk, pk = emoji_sets._operator()
    if not base or sk is None:
        return {"published": False, "skipped": "no public domain or operator key"}
    tags, content = build_tags_and_content(base)
    digest = hashlib.sha256(json.dumps([tags, content]).encode()).hexdigest()
    try:
        state = json.loads(settings_store.get(STATE_KEY, "") or "{}")
    except Exception:
        state = {}
    if not force and state.get("hash") == digest:
        return {"published": False, "skipped": "unchanged"}
    ok = await emoji_sets._publish_local(nostr_event.build_event(sk, KIND_HANDLER, content, tags))
    if ok:   # only a publish the relay accepted is remembered; a refused one is retried next start
        settings_store.put(STATE_KEY, json.dumps({"hash": digest}))
    logger.info("[app-handler] kind-%d announcement %s", KIND_HANDLER, "published" if ok else "NOT accepted")
    return {"published": bool(ok), "address": f"{KIND_HANDLER}:{pk}:{D_TAG}"}
