"""Mail search defaults to all configured accounts and preserves result identity."""
import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.routers import mail as route
from app.services import mail_store

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
