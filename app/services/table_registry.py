"""Every app table this node keeps as a relay DocTable (#161) -- one list, relay-only from the first request.

`start_loading()` (port-3051 startup, as early as possible) starts each table's background load on a thread of its
own: after a restart a big table takes a while, and meanwhile point reads are answered one document at a time from
the relay and whole-table reads answer Loading (503 + Retry-After) -- never part of a table. There is no migration
here: a node's old Postgres rows are copied once, by hand, with scripts/migrate_tables_to_nostr.py.
"""
import logging

from app.services.doc_table import DocTable

logger = logging.getLogger(__name__)

TABLES = (
    # wave 1
    "reminders", "scheduled_posts", "saved_searches", "social_reply_map",
    "push_subs", "direct_push_msgs", "push_follow_seen", "push_sent_wraps",
    "blossom_blobs", "stream_vods", "fedi_puppets", "fedi_bridge_delivered",
    "external_storage", "api_keys", "api_key_index", "shared_files", "verification_tokens",
    # wave 2
    "bots", "user_settings", "conversations",
    # wave 3
    "users", "user_index", "exodus_wallets", "exodus_wallet_records", "instance_applications",
)


def start_loading(names=None) -> None:
    """Start every table's background load (idempotent)."""
    for name in names or TABLES:
        try:
            DocTable(name).start_background_load()
        except Exception as e:      # noqa: BLE001 -- a table that cannot start loading now is retried by its reads
            logger.warning("[tables] %s: background load not started: %s", name, e)
