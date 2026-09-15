"""Exercise the registered move API with an isolated encrypted-store boundary."""
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
# Import the actual router and calendar store, isolating unrelated auth/database/relay modules.
# This lets the mandatory desktop gate exercise the real endpoint without starting the app.
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch
import app.services

ROOT = Path(__file__).resolve().parents[1]

def _module(name, **attrs):
    module=ModuleType(name)
    module.__dict__.update(attrs)
    return module

def _load(name, path):
    spec=importlib.util.spec_from_file_location(name, path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _auth(): pass

def _db(): pass

_dependencies = {
    'app.auth':_module('app.auth', get_current_user=_auth),
    'app.database':_module('app.database', get_db=_db),
    'app.models':_module('app.models', User=type('User',(),{}), UserSetting=type('UserSetting',(),{})),
    'app.services.nostr_store':_module('app.services.nostr_store', user_storage_seckey=lambda *_:None),
    'app.services.settings_store':_module('app.services.settings_store'),
    'app.services.caldav_subscribe':_module('app.services.caldav_subscribe'),
}
with patch.dict(sys.modules, _dependencies):
    with patch.object(app.services, 'nostr_store', _dependencies['app.services.nostr_store'], create=True), \
         patch.object(app.services, 'settings_store', _dependencies['app.services.settings_store'], create=True):
        _store=_load('app.services._calendar_move_test_store', ROOT/'app/services/caldav_store.py')
    with patch.object(app.services, 'caldav_store', _store, create=True), \
         patch.object(app.services, 'caldav_subscribe', _dependencies['app.services.caldav_subscribe'], create=True):
        routes=_load('_calendar_move_test_router', ROOT/'app/routers/calendar.py')

ICS = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:one\r\nDTSTART:20260914T120000Z\r\nRRULE:FREQ=WEEKLY\r\nX-PRIVATE:keep\r\nSUMMARY:Meet\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'

@pytest.fixture
def move_api(monkeypatch):
    state = SimpleNamespace(rows={'a':[{'uid':'one','ics':ICS}], 'b':[]}, calls=[],
                            calendars=[{'id':'a'}, {'id':'b'}], fail_put=False, fail_delete=False,
                            stale_after_copy=False, unavailable=False, item_reads=0, fail_read=0, forgotten=[])
    user=SimpleNamespace(id=42, username='calendar-owner')
    async def cals(db, who, **kw):
        assert who is user and kw == {'strict':True}
        if state.unavailable: raise OSError('relay unavailable')
        return state.calendars
    async def items(db, who, cal, **kw):
        assert who is user and kw == {'strict':True}
        state.item_reads += 1
        if state.item_reads == state.fail_read: raise OSError('item read unavailable')
        return [dict(r) for r in state.rows[cal]]
    async def put(db, who, cal, uid, ics, component):
        assert who is user
        state.calls.append(('put',cal))
        if state.fail_put: return False
        state.rows[cal]=[{'uid':uid,'ics':ics}]
        if state.stale_after_copy: state.rows['a'][0]['ics']=ICS+'changed'
        return True
    async def delete(db, who, cal, uid):
        assert who is user
        state.calls.append(('delete',cal))
        if state.fail_delete: return False
        state.rows[cal]=[]
        return True
    for name, fn in [('list_calendars',cals),('get_items',items),('put_item',put),('delete_item',delete)]:
        monkeypatch.setattr(routes.caldav_store,name,fn)
    monkeypatch.setattr(routes,'_require_enabled',lambda:None)
    monkeypatch.setattr(routes,'_forget',lambda name:state.forgotten.append(name))
    app=FastAPI();app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user]=lambda:user
    app.dependency_overrides[routes.get_db]=lambda:None
    with TestClient(app) as client:
        yield state, lambda **changes: client.post('/api/calendar/items/move', json={
            'cal':'a','target':'b','uid':'one','ics':ICS,'original_ics':ICS,**changes})


def test_move_preserves_ics_and_retries_after_lost_success(move_api):
    s, move=move_api
    assert move().status_code==200
    assert s.calls==[('put','b'),('delete','a')]
    assert s.rows=={'a':[], 'b':[{'uid':'one','ics':ICS}]}
    assert move().status_code==200
    assert s.calls==[('put','b'),('delete','a')]


def test_failed_destination_never_deletes_original(move_api):
    s, move=move_api;s.fail_put=True
    assert move().status_code==502
    assert s.calls==[('put','b')]
    assert s.rows['a'][0]['ics']==ICS


def test_delete_failure_reports_partial_copy_and_retry_finishes(move_api):
    s, move=move_api;s.fail_delete=True
    response=move();assert response.status_code==502
    assert 'Copied' in response.json()['detail']
    assert s.rows['a'] and s.rows['b']
    s.fail_delete=False
    assert move().status_code==200
    assert s.calls==[('put','b'),('delete','a'),('delete','a')]


@pytest.mark.parametrize('condition,status', [('collision',409),('source_changed',409),
    ('subscribed',409),('removed',404),('unavailable',503),('invalid_uid',400)])
def test_move_refusals_write_nothing(move_api,condition,status):
    s, move=move_api;kwargs={}
    if condition=='collision':s.rows['b']=[{'uid':'one','ics':'other'}]
    if condition=='source_changed':s.rows['a'][0]['ics']='new edit'
    if condition=='subscribed':s.calendars[1]['subscribe']={'url':'https://example.test/cal.ics'}
    if condition=='removed':s.calendars.pop()
    if condition=='unavailable':s.unavailable=True
    if condition=='invalid_uid':kwargs['uid']='different'
    assert move(**kwargs).status_code==status
    assert s.calls==[]


def test_edit_during_copy_is_not_deleted(move_api):
    s, move=move_api;s.stale_after_copy=True
    assert move().status_code==409
    assert s.calls==[('put','b')]
    assert s.rows['a'][0]['ics']==ICS+'changed'


@pytest.mark.parametrize('read_number', [1, 2, 3])
def test_unavailable_item_read_never_deletes_source(move_api, read_number):
    s, move = move_api
    s.fail_read = read_number
    assert move().status_code == 503
    assert s.rows['a'][0]['ics'] == ICS
    if read_number < 3:
        assert s.calls == []
        assert s.rows['b'] == []
    else:
        assert s.calls == [('put', 'b')]
        assert s.rows['b'][0]['ics'] == ICS
        assert s.forgotten == ['calendar-owner']


def test_partial_copy_invalidates_calendar_cache(move_api):
    s, move = move_api
    s.fail_delete = True
    assert move().status_code == 502
    assert s.forgotten == ['calendar-owner']


def test_subscribed_source_cannot_be_moved(move_api):
    s, move = move_api
    s.calendars[0]['subscribe'] = {'url':'https://example.test/feed.ics'}
    assert move().status_code == 409
    assert s.calls == []


def test_same_calendar_move_does_not_delete_event(move_api):
    s, move = move_api
    assert move(target='a').status_code == 400
    assert s.calls == []
    assert s.rows['a'][0]['ics'] == ICS
