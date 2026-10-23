"""The node's mail stack, REAL from the router down to the wire, with only three things chosen by the test:

  * where the mail servers are -- a loopback SMTP sink and IMAP server (tests/mail_wire_servers.py), so the SSRF
    guard is told 127.0.0.1 is allowed (it rightly refuses loopback on a node);
  * the account's settings rows (user_settings_table: the mail account, the folder map) -- an in-memory table;
  * where an attachment's CIPHERTEXT is kept (Blossom's index and byte store) -- an in-memory dict. The AES-GCM
    encryption and decryption are the shipped mail_sync code.

Everything else ships: app/routers/mail.py, mail_service (smtplib/imaplib), mail_sync, mail_store -> nostr_store ->
the shipped relay (realnode_harness.Home, PosterChanDB) on loopback.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from tests.mail_wire_servers import ImapServer, SmtpSink

ACCOUNT = 'me@home.test'
PASSWORD = 'app-password'


def storage_sk(u) -> bytes:
    return hashlib.sha256(b'pc-mail-wire:' + str(getattr(u, 'nostr_npub', '')).encode()).digest()


class Settings:
    """user_settings_table, in memory: {(user_id, key): value}."""

    def __init__(self):
        self.rows = {}

    def install(self, mp):
        from app.services import user_settings_table as T

        def get(db, uid, key, default=None):
            return self.rows.get((uid, key), default)

        async def aget(db, uid, key, default=None):
            return get(db, uid, key, default)

        def set_(db, uid, key, value):
            self.rows[(uid, key)] = value

        async def aset(db, uid, key, value):
            set_(db, uid, key, value)

        async def ahas(db, uid, key):
            return (uid, key) in self.rows

        async def adelete(db, uid, key):
            self.rows.pop((uid, key), None)
        for name, fn in (('get', get), ('aget', aget), ('set', set_), ('aset', aset), ('ahas', ahas),
                         ('adelete', adelete)):
            mp.setattr(T, name, fn)


class Blobs:
    """Blossom's index + bytes for the mail attachments (ciphertext only ever reaches here)."""

    def __init__(self):
        self.data = {}

    def install(self, mp):
        from app.services import blossom_service, mail_sync

        async def save(owner_pubkey, data, mime='application/octet-stream'):
            sha = hashlib.sha256(data).hexdigest()
            self.data[sha] = data
            return {'sha256': sha, 'size': len(data)}

        async def meta(db, sha):
            return {'sha256': sha} if sha in self.data else None

        async def read_full(db, blob):
            return self.data.get(blob['sha256'])
        mp.setattr(mail_sync, '_save_blob', save)
        mp.setattr(blossom_service, 'get_blob_meta', meta)
        mp.setattr(blossom_service, 'read_full', read_full)


def account_row(smtp_port, imap_port, email=ACCOUNT, password=PASSWORD):
    return {'email': email, 'password': password, 'imap_server': '127.0.0.1', 'imap_port': imap_port,
            'smtp_server': '127.0.0.1', 'smtp_port': smtp_port, 'use_ssl': False}


class MailWire:
    """The servers + the patches. `users`: the accounts that have this mailbox configured (each its own copy)."""

    def __init__(self, mp, relay_port, users):
        from app.routers import mail as mail_router
        from app.services import mail_service, mail_store, nostr_store
        self.smtp = SmtpSink(user=ACCOUNT, password=PASSWORD)
        self.imap = ImapServer(user=ACCOUNT, password=PASSWORD)
        self.settings = Settings()
        self.settings.install(mp)
        self.blobs = Blobs()
        self.blobs.install(mp)
        real_validate = mail_service.validate_mail_server
        mp.setattr(mail_service, 'validate_mail_server',
                   lambda h, p: True if h == '127.0.0.1' else real_validate(h, p))
        mp.setattr(mail_store, '_port', lambda: relay_port)
        mp.setattr(nostr_store, 'user_storage_seckey', lambda db, u: storage_sk(u))
        mail_router._THREAD_SCAN.clear() if hasattr(mail_router, '_THREAD_SCAN') else None
        for u in users:
            self.configure(u)

    def configure(self, u, smtp_port=None, imap_port=None):
        self.settings.rows[(u.id, 'mail_accounts')] = json.dumps(
            [account_row(smtp_port or self.smtp.port, imap_port or self.imap.port)])

    def stop(self):
        self.smtp.stop()
        self.imap.stop()


def user(i, pk=None):
    pk = pk or ('%02x' % i) * 32
    return SimpleNamespace(id=4000 + i, username='mw%d' % i, nostr_npub=pk, is_admin=False,
                           email='mw%d@example.invalid' % i)
