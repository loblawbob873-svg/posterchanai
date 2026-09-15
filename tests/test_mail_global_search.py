"""Mail search defaults to all configured accounts and preserves result identity."""
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
    mail_store = _load('_global_search_test_store', ROOT / 'app/services/mail_store.py')
    stack.enter_context(patch.object(app.services, 'mail_store', mail_store, create=True))
    route = _load('_global_search_test_router', ROOT / 'app/routers/mail.py')

@pytest.mark.parametrize('account',['','__all'])
def test_global_search_finds_second_account_archive_and_excludes_removed_accounts(monkeypatch,account):
    monkeypatch.setattr(route,'_seckey',lambda db,user:b'x'*32)
    monkeypatch.setattr(route,'get_user_mail_accounts',lambda uid,db:[SimpleNamespace(email=e) for e in ['same@work.test','same@home.test']])
    calls=[]
    async def scan(sk,acct,folder):
        calls.append((acct,folder))
        return [dict(uid='7',account='same@work.test',folder='INBOX',subject='Invoice',ts=10),
                dict(uid='7',account='same@home.test',folder='Archive',subject='Invoice',ts=20),
                dict(uid='8',account='removed@test',folder='INBOX',subject='Invoice',ts=30)]
    monkeypatch.setattr(mail_store,'list_all_messages',scan)
    result=asyncio.run(route.mail_search('invoice',account,'',None,SimpleNamespace(id=42)))
    assert calls==[(None,None)]
    assert [(m['account'],m['folder'],m['uid']) for m in result['messages']]==[('same@home.test','Archive','7'),('same@work.test','INBOX','7')]


def test_invalid_explicit_account_does_not_expand_to_all_mail(monkeypatch):
    monkeypatch.setattr(route,'_seckey',lambda db,user:b'x'*32)
    monkeypatch.setattr(route,'get_user_mail_accounts',lambda uid,db:[SimpleNamespace(email='configured@test')])
    monkeypatch.setattr(route,'_resolve_account',lambda *args:None)
    async def scan(*args):raise AssertionError('invalid account must not scan')
    monkeypatch.setattr(mail_store,'list_all_messages',scan)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.mail_search('invoice','invalid@test','',None,SimpleNamespace(id=42)))
    assert caught.value.status_code==404


def test_explicit_account_search_keeps_its_folder_scope(monkeypatch):
    monkeypatch.setattr(route,'_seckey',lambda db,user:b'x'*32)
    account=SimpleNamespace(email='configured@test')
    monkeypatch.setattr(route,'get_user_mail_accounts',lambda uid,db:[account])
    calls=[]
    async def scan(sk,acct,folder):
        calls.append((acct,folder))
        return [dict(uid='1',account=acct,folder=folder,subject='Invoice',ts=1)]
    monkeypatch.setattr(mail_store,'list_all_messages',scan)
    result=asyncio.run(route.mail_search('invoice','configured@test','Sent',None,SimpleNamespace(id=42)))
    assert calls==[('configured@test','Sent')]
    assert result['messages'][0]['folder']=='Sent'


def test_blank_search_does_not_scan_mailboxes(monkeypatch):
    monkeypatch.setattr(route,'_seckey',lambda db,user:b'x'*32)
    monkeypatch.setattr(route,'get_user_mail_accounts',lambda uid,db:[SimpleNamespace(email='configured@test')])
    async def scan(*args):raise AssertionError('blank search must not scan')
    monkeypatch.setattr(mail_store,'list_all_messages',scan)
    assert asyncio.run(route.mail_search('  ','','',None,SimpleNamespace(id=42)))=={'messages':[]}


def test_registered_search_route_defaults_to_global_scope(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(route, '_seckey', lambda *args: b'x' * 32)
    monkeypatch.setattr(route, 'get_user_mail_accounts', lambda *args: [SimpleNamespace(email='second@test')])
    async def scan(key, account, folder):
        assert account is None and folder is None
        return [dict(uid='1', account='second@test', folder='Archive', subject='Invoice', ts=1)]
    monkeypatch.setattr(mail_store, 'list_all_messages', scan)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.get_db] = lambda: None
    app.dependency_overrides[route.get_instance_user] = lambda: SimpleNamespace(id=42)
    with TestClient(app) as client:
        response = client.get('/api/mail/search', params={'q': 'invoice'})
    assert response.status_code == 200
    assert response.json()['messages'][0]['account'] == 'second@test'
