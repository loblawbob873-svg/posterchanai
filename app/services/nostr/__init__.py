"""Nostr support: pure-Python BIP340/bech32 + relay/media clients + a high-level facade.

Most callers want the facade: `from app.services.nostr import nostr_service`.
"""

from . import bech32, bip340, event, relay, media, nostr_service
from .nostr_service import DEFAULT_RELAYS

__all__ = ["bech32", "bip340", "event", "relay", "media", "nostr_service", "DEFAULT_RELAYS"]
