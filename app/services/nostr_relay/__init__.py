"""Built-in Nostr web-of-trust relay (NIP-01/09/11), self-contained in PosterChanAI.

Runs in its own daemon thread + asyncio loop (see thread.py); stores into a tmpfs SQLite
hot DB with periodic disk snapshots; only accepts/syncs events from a web-of-trust set.
"""

from .thread import start_nostr_relay, stop_nostr_relay

__all__ = ["start_nostr_relay", "stop_nostr_relay"]
