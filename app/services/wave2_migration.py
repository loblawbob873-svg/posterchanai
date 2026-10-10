"""One-time copy of the wave-2 app tables from Postgres into their DocTables (#161): bots, user_settings,
conversations -- plus a read-only census of the legacy `messages` rows.

Each table goes through `doc_table_bulk.amigrate_rows`: copy what differs, remove documents no SQL row accounts
for, RE-READ the table strictly from the relay and compare row for row, and only then write the `_migrated`
marker. Two things are specific to these tables, because they stay writable while the copy runs (table_gate):

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

from app.services import doc_table_bulk
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

TABLES = ("bots", "user_settings", "conversations")
MESSAGES = "messages"


def _sources():
    from app.services import bot_table, conversation_table, user_settings_table
    return {bot_table.TABLE: bot_table.sql_rows,
            user_settings_table.TABLE: user_settings_table.sql_rows,
            conversation_table.TABLE: conversation_table.sql_rows}


def _read(session_factory, fn) -> dict:
    db = session_factory()
    try:
        return {str(k): doc_table_bulk._norm(v) for k, v in fn(db).items()}
    finally:
        try:
            db.rollback()
        finally:
            db.close()


async def migrate_table(name: str, session_factory) -> dict:
    """Copy table `name` once; returns the report. Raises MigrationMismatch (no marker) or Unavailable."""
    fn = _sources()[name]
    m = await doc_table_bulk.amarker(name)
    if m and m.get("verified"):
        doc_table_bulk._seen.add(name)
        return {"table": name, "skipped": True}
    rows = await asyncio.to_thread(_read, session_factory, fn)

    async def unchanged() -> bool:
        return (await asyncio.to_thread(_read, session_factory, fn)) == rows

    return await doc_table_bulk.amigrate_rows(name, rows, before_mark=unchanged)


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
    """Count legacy SQL messages with and without a relay counterpart (matched per conversation by role +
    content, as a multiset). Writes the `messages` marker with the counts. Raises Unavailable when any
    conversation's relay documents could not be read -- a census with holes is not written."""
    from collections import Counter
    from app.services import nostr_store
    from app.services.doc_table import DocTable
    from app.services.relay_reader import relay_port
    m = await doc_table_bulk.amarker(MESSAGES)
    if m and m.get("verified"):
        return {"table": MESSAGES, "skipped": True}

    def _collect():
        db = session_factory()
        try:
            legacy = _legacy_messages(db)
            keys = {cid: _storage_key_readonly(db, e["user_id"], e["npub"]) for cid, e in legacy["convs"].items()}
            return legacy, keys
        finally:
            db.close()

    legacy, keys = await asyncio.to_thread(_collect)
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
    info = {"verified": True, "census_only": True, "sql_rows": sql_rows, "on_relay": on_relay,
            "not_on_relay": missing, "orphan_rows": legacy["orphans"],
            "conversations": len(legacy["convs"]), "conversations_with_gaps": gaps,
            "conversations_without_storage_key": no_key,
            "note": "relay-authoritative since c3ed195a2; legacy rows are never copied (they cannot be told "
                    "apart from history deleted on the relay)"}
    await DocTable(doc_table_bulk.MARKER_TABLE).aput(MESSAGES, info)
    doc_table_bulk._seen.add(MESSAGES)
    logger.info("[wave2-migration] messages census: %d legacy row(s), %d on the relay, %d not (in %d "
                "conversation(s)) -- none copied", sql_rows, on_relay, missing, len(gaps))
    return dict(info, table=MESSAGES)


# --------------------------------------------------------------------------- startup
async def migrate_all(session_factory, names=None) -> dict:
    """Every table in turn; one failing does not stop the others. {name: report | error string}."""
    out = {}
    for name in names or (TABLES + (MESSAGES,)):
        try:
            if name == MESSAGES:
                out[name] = await amessage_census(session_factory)
            else:
                out[name] = await migrate_table(name, session_factory)
        except doc_table_bulk.MigrationMismatch as e:
            out[name] = "did not verify: %s" % e
            logger.error("[wave2-migration] %s", e)
        except Exception as e:      # noqa: BLE001 -- Unavailable, a SQL error: no verdict, retried
            out[name] = "not migrated: %s" % e
            logger.warning("[wave2-migration] %s not migrated yet: %s", name, type(e).__name__)
    return out


def _done(report) -> bool:
    return isinstance(report, dict) and (report.get("skipped") or report.get("verified"))


async def run_at_startup(session_factory, retry_s: float = 30.0) -> dict:
    """The port-3051 startup pass: migrate, retried until every table has its marker, then load each table
    (strictly) and tell the relay thread to re-read its operator set (bot keys come from the bots table)."""
    from app.services.doc_table import DocTable
    pending = list(TABLES + (MESSAGES,))
    out = {}
    while pending:
        out.update(await migrate_all(session_factory, pending))
        for name in [n for n in pending if _done(out.get(n))]:
            pending.remove(name)
            if name in TABLES:
                try:
                    await DocTable(name).aload()
                except Unavailable as e:
                    logger.warning("[wave2-migration] %s migrated but not loaded yet: %s", name, e)
                if name == "bots":
                    try:
                        from app.services.nostr_relay.thread import trigger_block_reload
                        trigger_block_reload()
                    except Exception:      # noqa: BLE001
                        pass
        if pending:
            await asyncio.sleep(retry_s)
    return out
