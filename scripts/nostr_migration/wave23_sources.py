"""SQL row readers for the wave-2 and wave-3 tables (#161): each turns a node's OLD Postgres rows into EXACTLY the
row dicts the relay-only app stores (the same keys, the same shapes -- built with the app's own row classes), for
scripts/migrate_tables_to_nostr.py to copy once.

    SOURCES = {doc_table_name: fn(db) -> {key: row}}
    DERIVED = {doc_table_name: fn(sources_rows: {doc_table_name: {key: row}}) -> {key: row}}

Read-only: every function only SELECTs. Ids are kept (accounts, conversations and bots keep their SQL ids -- a
session, a chat transcript and the Admin UI's URLs carry them).

NOT copied, on purpose: the legacy chat `messages` rows. Since c3ed195a2 (2026-07-22) a transcript is relay-only --
one encrypted document per message under the user's storage key -- and what is left in SQL is the plaintext copy
from before that date. It cannot be told apart from history the user has since DELETED on the relay (Telegram's
`new` and the web UI's delete remove the relay documents and never touched those rows), so copying "what the relay
lacks" would bring deleted conversations back.
"""
from __future__ import annotations

from sqlalchemy import Column, Integer, String, Text

from app.database import Base


class InstanceApplication(Base):
    """The old `instance_applications` table (its model left app/services/instance_welcome.py with the move)."""
    __tablename__ = "instance_applications"
    __table_args__ = {"extend_existing": True}
    pubkey = Column(String(64), primary_key=True)
    created_at = Column(Integer, nullable=False)
    admin_notified = Column(Integer, nullable=False, default=0)
    notified_admins = Column(Text, nullable=False, default="[]")
    admin_retry_at = Column(Integer, nullable=False, default=0)
    approval_retry_at = Column(Integer, nullable=False, default=0)
    approved_address = Column(String(320), nullable=False, default="")
    notified_address = Column(String(320), nullable=False, default="")


# ------------------------------------------------------------------------------------ wave 2
def bots(db) -> dict:
    """key = str(id)"""
    from app.models import Bot
    from app.services.bot_table import FIELDS, BotRow
    return {str(b.id): BotRow(b.id, **{f: getattr(b, f) for f in FIELDS}, created_at=b.created_at,
                              updated_at=b.updated_at).row()
            for b in db.query(Bot).all()}


def user_settings(db) -> dict:
    """key = "<user_id>:<key>". Two SQL rows for one (user, key) (the table never had a unique constraint)
    collapse to the NEWEST (highest id): it is the one the last save wrote."""
    from app.models import UserSetting
    from app.services.user_settings_table import _row, row_key
    out = {}
    for r in db.query(UserSetting).order_by(UserSetting.id.asc()).all():
        out[row_key(r.user_id, r.key)] = _row(r.user_id, r.key, r.value)
    return out


def conversations(db) -> dict:
    """key = str(id)"""
    from app.models import Conversation
    from app.services.conversation_table import Conv
    return {str(c.id): Conv(c.id, c.user_id, c.title, c.created_at, c.updated_at).row()
            for c in db.query(Conversation).all()}


# ------------------------------------------------------------------------------------ wave 3
def _user_columns():
    from app.models import User
    from app.services import user_table
    sql_cols = tuple(c.name for c in User.__table__.columns if c.name != "id")
    if sql_cols != user_table.COLUMNS:
        raise RuntimeError("the users model and user_table.COLUMNS disagree: model-only %s, table-only %s"
                           % (sorted(set(sql_cols) - set(user_table.COLUMNS)),
                              sorted(set(user_table.COLUMNS) - set(sql_cols))))
    return user_table.COLUMNS


def users(db) -> dict:
    """key = str(id); exactly user_table.COLUMNS (password hash, linked nsec and link key included -- the
    documents are NIP-44-encrypted to the operator key and never leave this node)."""
    from app.models import User
    from app.services import app_tables, user_table
    cols = _user_columns()
    out = {}
    for u in db.query(User).order_by(User.id.asc()).all():
        out[str(u.id)] = {c: (app_tables.to_iso(getattr(u, c)) if c in user_table._DATETIMES else getattr(u, c))
                          for c in cols}
    return out


def user_index(src: dict) -> dict:
    """key = sha256(field, value) -> {"field", "ids"} -- the lookup index a point lookup reads during a load."""
    from app.services import user_table
    return user_table.index_rows(src["users"])


def exodus_wallets(db) -> dict:
    """key = str(user_id); the sealed seed as hex, exactly as exodus_tables writes it."""
    from app.models import ExodusWallet
    from app.services import app_tables
    return {str(int(w.user_id)): {"user_id": int(w.user_id),
                                  "seed_enc": bytes(w.seed_enc).hex() if w.seed_enc is not None else None,
                                  "label": w.label, "address_index": int(w.address_index or 0),
                                  "backed_up_at": app_tables.to_iso(w.backed_up_at),
                                  "created_at": app_tables.to_iso(w.created_at)}
            for w in db.query(ExodusWallet).all()}


def exodus_wallet_records(db) -> dict:
    """key = "<user_id>:<wallet_id>"."""
    from app.models import ExodusWalletRecord
    from app.services.exodus_tables import record_key
    return {record_key(r.user_id, r.wallet_id): {
                "user_id": int(r.user_id), "wallet_id": r.wallet_id,
                "document_enc": bytes(r.document_enc).hex() if r.document_enc is not None else None}
            for r in db.query(ExodusWalletRecord).all()}


def instance_applications(db) -> dict:
    """key = pubkey."""
    from app.services.instance_welcome import FIELDS, Application
    return {r.pubkey: Application(r.pubkey, {f: getattr(r, f) for f in FIELDS}).row()
            for r in db.query(InstanceApplication).all()}


SOURCES = {
    "bots": bots,
    "user_settings": user_settings,
    "conversations": conversations,
    "users": users,
    "exodus_wallets": exodus_wallets,
    "exodus_wallet_records": exodus_wallet_records,
    "instance_applications": instance_applications,
}
DERIVED = {
    "user_index": user_index,           # needs SOURCES["users"]
}
DERIVED_FROM = {"user_index": "users"}
