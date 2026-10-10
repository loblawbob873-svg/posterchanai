"""Email delete: one IMAP session for a batch, the Trash name remembered, and a refusal is SAID.

"Email: deletes are slow". Per message the server opened TWO IMAP sessions -- one only to LIST the
folders and learn the Trash folder's name, one to SELECT/COPY/STORE/EXPUNGE -- and a bulk delete
was one HTTP request per message, so N messages cost 2N logins in series. And every IMAP failure was
swallowed: the local copy was dropped and `ok` returned, so the message vanished and came back on
the next sync. These drive the shipped route with the IMAP layer replaced by counters."""
import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
# Load the shipped router/store while isolating unrelated database, credentials and
# network services. The release gate deliberately has no application database.
import importlib.util
from contextlib import ExitStack
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch
import app.services

ROOT = Path(__file__).resolve().parents[1]

def _blocked(*args, **kwargs):
    raise AssertionError('unexpected external service access')

def _module(name, **attrs):
    module = ModuleType(name)
    module.__dict__.update(attrs)
    return module

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_services = {
    'nostr_store': _module('nostr_store', user_storage_seckey=_blocked),
    'settings_store': _module('settings_store'),
    'mail_sync': _module('mail_sync'),
    'instance_membership': _module('instance_membership', require_user=_blocked),
    'mail_service': _module('mail_service', **dict.fromkeys((
        'get_attachment', 'get_user_mail_accounts', 'sanitize_filename', 'send_email',
        'reply_to_message', 'forward_message', 'archive_message', 'delete_message',
        'move_message', 'list_special_folders'), _blocked)),
    'storage_service': _module('storage_service', StorageService=_blocked,
        _sanitize_path_component=_blocked, _validate_path_within_base=_blocked),
}
_dependencies = {
    'app.auth': _module('app.auth', get_current_user=_blocked),
    'app.database': _module('app.database', get_db=_blocked),
    'app.models': _module('app.models', User=type('User', (), {})),
    'app.services.nostr': _module('app.services.nostr', nostr_service=_module('nostr_service')),
    **{'app.services.' + name: module for name, module in _services.items()},
}
with ExitStack() as stack:
    stack.enter_context(patch.dict(sys.modules, _dependencies))
    stack.enter_context(patch.object(app.services, 'nostr', _dependencies['app.services.nostr'], create=True))
    for name, module in _services.items():
        stack.enter_context(patch.object(app.services, name, module, create=True))
    mail_store = _load('_delete_batch_test_store', ROOT / 'app/services/mail_store.py')
    stack.enter_context(patch.object(app.services, 'mail_store', mail_store, create=True))
    route = _load('_delete_batch_test_router', ROOT / 'app/routers/mail.py')



class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


def _wire(monkeypatch, *, move_ok=True, delete_ok=True, archive_ok=lambda uid: True):
    calls = {'list': 0, 'move': [], 'delete': [], 'archive': [], 'mirror': []}
    getattr(route, "_trash_cache", {}).clear()
    monkeypatch.setattr(route, '_seckey', lambda db, user: b'x' * 32)
    monkeypatch.setattr(route, '_resolve_account', lambda db, user, hint: SimpleNamespace(email='me@x.test'))

    def special(uid, db, email):
        calls['list'] += 1
        return {'trash': 'Trash'}
    monkeypatch.setattr(route, '_list_special_folders', special)

    def move(uid_, db, email, uid, src, dest):
        calls['move'].append((uid, src, dest))
        return move_ok
    monkeypatch.setattr(route, 'move_message', move)

    def delete(uid_, db, email, uid, folder):
        calls['delete'].append((uid, folder))
        return delete_ok
    monkeypatch.setattr(route, 'delete_message', delete)

    def archive(uid_, db, email, uid, folder):
        calls['archive'].append(uid)
        return archive_ok(uid)
    monkeypatch.setattr(route, 'archive_message', archive)

    async def mirror(sk, email, folder, uid):
        calls['mirror'].append(uid)
        return True
    monkeypatch.setattr(mail_store, 'delete_message', mirror)
    return calls


def test_a_batch_is_one_imap_move_and_the_trash_name_is_remembered(monkeypatch):
    calls = _wire(monkeypatch)
    out = asyncio.run(route.mail_delete(_Req({'account': 'me@x.test', 'folder': 'INBOX', 'uids': ['3', '4', '5']}),
                                        None, SimpleNamespace(id=1)))
    assert out['ok'] is True
    assert calls['move'] == [('3,4,5', 'INBOX', 'Trash')], 'a batch must be ONE UID COPY of the whole set'
    assert sorted(calls['mirror']) == ['3', '4', '5']
    asyncio.run(route.mail_delete(_Req({'account': 'me@x.test', 'folder': 'INBOX', 'uid': '9'}),
                                  None, SimpleNamespace(id=1)))
    assert calls['move'][-1] == ('9', 'INBOX', 'Trash'), 'the single-uid form older clients send still works'
    assert calls['list'] == 1, 'the Trash folder name was looked up again (a whole IMAP session per delete)'


def test_a_delete_the_mail_server_refuses_is_an_error_and_keeps_the_local_copy(monkeypatch):
    calls = _wire(monkeypatch, move_ok=False, delete_ok=False)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.mail_delete(_Req({'account': 'me@x.test', 'folder': 'INBOX', 'uids': ['3']}),
                                      None, SimpleNamespace(id=1)))
    assert caught.value.status_code == 502
    assert calls['mirror'] == [], 'the local copy was dropped for a message the server still holds'


def test_non_numeric_uids_are_never_joined_into_an_imap_set(monkeypatch):
    calls = _wire(monkeypatch)
    asyncio.run(route.mail_delete(_Req({'account': 'me@x.test', 'folder': 'INBOX', 'uids': ['1', '2 FLAGS']}),
                                  None, SimpleNamespace(id=1)))
    assert [m[0] for m in calls['move']] == ['1', '2 FLAGS']


def test_a_partly_refused_archive_names_what_stayed(monkeypatch):
    calls = _wire(monkeypatch, archive_ok=lambda uid: uid != '4')
    out = asyncio.run(route.mail_archive(_Req({'account': 'me@x.test', 'folder': 'INBOX', 'uids': ['3', '4']}),
                                         None, SimpleNamespace(id=1)))
    assert out['ok'] is False and out['failed'] == ['4'], out
    assert calls['mirror'] == ['3'], 'only what was archived leaves the local mailbox'
    calls = _wire(monkeypatch, archive_ok=lambda uid: False)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.mail_archive(_Req({'account': 'me@x.test', 'folder': 'INBOX', 'uid': '3'}),
                                       None, SimpleNamespace(id=1)))
    assert caught.value.status_code == 502 and calls['mirror'] == []
