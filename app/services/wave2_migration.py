"""The wave-2 app tables of #161 -- bots, user_settings, conversations -- plus the read-only census of the legacy
`messages` rows, REGISTERED WITH THE ONE MIGRATION (app/services/table_migration.py): the same `_migrated`
marker, the same engine (`doc_table_bulk.amigrate_rows`), the same port-3051 startup pass and the same script
(`scripts/migrate_tables_to_nostr.py --table bots`) as every wave-1 table.

What differs is how the STORE gates SQL-or-relay. A wave-1 table binds a `doc_table.Legacy` and DocTable itself
reads SQL and writes SQL-then-relay until the marker. These three keep their own gate (table_gate): the store
writes the relay FIRST and SQL after it, and no Legacy is bound to the DocTable (they register as GATED). So their
copy is a SNAPSHOT plus a re-read before the marker:

  * before the marker, SQL is re-read; if it changed while the copy ran (a write landed relay-first, and the copy
    may then have put the older SQL value back over it) the marker is withheld and the next pass tries again;
  * the SQL rows are only ever SELECTed. Nothing here drops a table or deletes a row.

MESSAGES ARE NOT COPIED, deliberately. Since c3ed195a2 (2026-07-22) a transcript is relay-only -- one encrypted
document per message under the user's storage key -- and what is left in the SQL `messages` table is the
plaintext copy from before that date. It cannot be told apart from history the user has since DELETED on the
relay (Telegram's `new` and the web UI's delete remove the relay documents and never touched those SQL rows), so
copying "what the relay lacks" would bring deleted conversations back. `amessage_census` counts, per
conversation, how many legacy rows have no relay counterpart and records that (counts and ids only -- never
content) as the `messages` marker, so an operator can see it before the plaintext table is purged.
"""
import asyncio
import hashlib
import logging

from app.services import doc_table, doc_table_bulk
from app.services.doc_table import Legacy
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

TABLES = ("bots", "user_settings", "conversations")
MESSAGES = "messages"


def _sources():
    from app.services import bot_table, conversation_table, user_settings_table
    return {bot_table.TABLE: bot_table.sql_rows,
            user_settings_table.TABLE: user_settings_table.sql_rows,
            conversation_table.TABLE: conversation_table.sql_rows}


def _runner(session_factory):
    """`run(fn)` = fn(db) in a session of its own from `session_factory` (read only: rolled back, closed)."""
    def run(fn):
        db = session_factory()
        try:
            return fn(db)
        finally:
            try:
                db.rollback()
            finally:
                db.close()
    return run


async def acopy_gated(name: str, sql, *, in_thread: bool = True) -> dict:
    """The engine run for one GATED table: `sql(fn)` runs fn(db) against SQL. Copies a snapshot, verifies it on the
    relay, and before the marker re-reads SQL -- a change since the snapshot withholds the marker
    (MigrationMismatch); the next pass copies the newer rows."""
    fn = _sources()[name]

    def read():
        return {str(k): doc_table_bulk._norm(v) for k, v in sql(fn).items()}

    async def call():
        return await asyncio.to_thread(read) if in_thread else read()

    m = await doc_table_bulk.amarker(name)
    if doc_table.is_marker(m):
        doc_table._saw_marked(name)
        return dict(m, table=name, skipped=True)
    rows = await call()

    async def unchanged() -> bool:
        return (await call()) == rows

    return await doc_table_bulk.amigrate_rows(name, rows, before_mark=unchanged)


async def migrate_table(name: str, session_factory) -> dict:
    """Copy table `name` once through the one engine; returns the report. Raises MigrationMismatch (no marker) or
    Unavailable."""
    from app.services import table_migration
    return await table_migration.amigrate(name, session_factory=session_factory)


# --------------------------------------------------------------------------- messages: census only
def _fingerprint(role, content) -> str:
    return hashlib.sha256(("%s\0%s" % (role or "", content or "")).encode()).hexdigest()


def _legacy_messages(db) -> dict:
    """{conversation_id: {"user_id", "npub", "prints": [fingerprint, ...]}} for every SQL message row."""
    from app.models import Conversation, Message, User
    out = {}
    q = (db.query(Message.conversation_id, Message.role, Message.content, Conversation.user_id, User.nostr_npub)
         .join(Conversation, Conversation.id == Message.conversation_id)
         .outerjoin(User, User.id == Conversation.user_id))
    for conv_id, role, content, user_id, npub in q.all():
        e = out.setdefault(int(conv_id), {"user_id": user_id, "npub": npub, "prints": []})
        e["prints"].append(_fingerprint(role, content))
    orphans = db.query(Message.id).outerjoin(Conversation, Conversation.id == Message.conversation_id) \
        .filter(Conversation.id.is_(None)).count()
    return {"convs": out, "orphans": orphans}


def _storage_key_readonly(db, user_id, npub):
    """The user's storage key WITHOUT minting one (user_storage_seckey would generate a key for a user who has
    none -- a census must not write)."""
    from app.services import keystore
    if npub:
        sk = keystore.get_storage_seckey(npub)
        if sk:
            return sk
    from app.services import user_settings_table
    value = user_settings_table.get(db, user_id, "storage_nsec")     # runs off the loop (to_thread)
    if value:
        try:
            return bytes.fromhex(value)
        except ValueError:
            return None
    return None


async def amessage_census(session_factory) -> dict:
    """The census with SQL sessions from `session_factory` (see `acensus`)."""
    return await acensus(_runner(session_factory))


async def acensus(sql, *, in_thread: bool = True) -> dict:
    """Count legacy SQL messages with and without a relay counterpart (matched per conversation by role +
    content, as a multiset). Writes the `messages` marker with the counts. Raises Unavailable when any
    conversation's relay documents could not be read -- a census with holes is not written."""
    from collections import Counter
    from app.services import nostr_store
    from app.services.relay_reader import relay_port
    m = await doc_table_bulk.amarker(MESSAGES)
    if doc_table.is_marker(m):
        doc_table._saw_marked(MESSAGES)
        return dict(m, table=MESSAGES, skipped=True)

    def _collect(db):
        legacy = _legacy_messages(db)
        keys = {cid: _storage_key_readonly(db, e["user_id"], e["npub"]) for cid, e in legacy["convs"].items()}
        return legacy, keys

    # always on a thread, even for a caller that asks for in_thread=False: `_storage_key_readonly` asks the
    # user_settings gate synchronously, which on the event loop can only answer from memory
    legacy, keys = await asyncio.to_thread(sql, _collect)
    on_relay = missing = 0
    gaps, no_key = {}, []
    for cid, e in sorted(legacy["convs"].items()):
        sk = keys.get(cid)
        if sk is None:
            no_key.append(cid)
            missing += len(e["prints"])
            gaps[str(cid)] = len(e["prints"])
            continue
        try:
            docs = await nostr_store.list_all_docs(relay_port(), "%s%d:" % (nostr_store.NS_MSG, cid), seckey=sk)
        except Exception as ex:      # noqa: BLE001
            raise Unavailable("could not read conversation %d's transcript: %s" % (cid, ex)) from ex
        have = Counter(_fingerprint(v.get("role"), v.get("content")) for v in docs.values() if isinstance(v, dict))
        want = Counter(e["prints"])
        lacking = sum((want - have).values())
        on_relay += sum(want.values()) - lacking
        missing += lacking
        if lacking:
            gaps[str(cid)] = lacking
    sql_rows = on_relay + missing
    info = {"ok": True, "verified": True, "census_only": True, "sql_rows": sql_rows, "on_relay": on_relay,
            "not_on_relay": missing, "orphan_rows": legacy["orphans"],
            "conversations": len(legacy["convs"]), "conversations_with_gaps": gaps,
            "conversations_without_storage_key": no_key,
            "note": "relay-authoritative since c3ed195a2; legacy rows are never copied (they cannot be told "
                    "apart from history deleted on the relay)"}
    await doc_table_bulk._write_marker(MESSAGES, info)
    logger.info("[wave2-migration] messages census: %d legacy row(s), %d on the relay, %d not (in %d "
                "conversation(s)) -- none copied", sql_rows, on_relay, missing, len(gaps))
    return dict(info, table=MESSAGES)


# --------------------------------------------------------------------------- the registry
async def migrate_all(session_factory, names=None) -> dict:
    """Every wave-2 table in turn through the one engine; one failing does not stop the others.
    {name: report | error string}."""
    from app.services import table_migration
    out = {}
    for name in names or (TABLES + (MESSAGES,)):
        out.update(await table_migration.amigrate_all([name], session_factory=session_factory))
    return out


class GatedLegacy(Legacy):
    """A wave-2 table in table_migration's registry. `gated`: the store gates SQL-or-relay itself (table_gate), so
    no Legacy is bound to its DocTable; `amigrate` is how the one engine copies it."""
    gated = True

    def __init__(self, name: str):
        self.name = name

    def rows(self, db) -> dict:
        return _sources()[self.name](db)

    async def amigrate(self, sql, *, in_thread: bool = True) -> dict:
        return await acopy_gated(self.name, sql, in_thread=in_thread)

    def after_marked(self) -> None:
        """Called by the startup pass (a thread of its own, off any event loop) once the table is marked: load it
        now, and for `bots` have the relay re-read its operator set (the bots' keys) from the loaded table."""
        try:
            doc_table.DocTable(self.name).load()
        except Exception as e:      # noqa: BLE001 -- the background loader keeps trying
            logger.warning("[wave2-migration] %s marked but not loaded yet: %s", self.name, type(e).__name__)
            return
        if self.name == "bots":
            try:
                from app.services.nostr_relay.thread import trigger_block_reload
                trigger_block_reload()
            except Exception:      # noqa: BLE001
                pass


class CensusLegacy(Legacy):
    """The legacy chat `messages` rows: counted, never copied (`acensus`). No DocTable is loaded for it."""
    copy = False

    def __init__(self):
        self.name = MESSAGES

    def rows(self, db) -> dict:
        return {}

    async def amigrate(self, sql, *, in_thread: bool = True) -> dict:
        return await acensus(sql, in_thread=in_thread)


def _register() -> None:
    from app.services import table_migration
    for name in TABLES:
        table_migration.register(GatedLegacy(name))
    table_migration.register(CensusLegacy())


_register()
