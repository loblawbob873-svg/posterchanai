import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import Base,User,FediPuppet,Bot
from app.services import relay_access_policy as policy,nostr_dvm
from app.services.nostr import nostr_service as ns

@pytest.fixture
def world(monkeypatch):
    from sqlalchemy.pool import StaticPool
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine,tables=[User.__table__,FediPuppet.__table__,Bot.__table__])
    db=Session(engine)
    keys=[('%064x'%i) for i in range(1,7)]
    config={'nostr_relay_nip05_domain':'poster.place','nostr_relay_nip05_names':'local '+ns.npub_of(keys[0])}
    monkeypatch.setattr(policy.settings,'hydrate_from_db',lambda db:None)
    monkeypatch.setattr(policy.settings,'is_hydrated',lambda:True)
    monkeypatch.setattr(policy.settings,'get',lambda key,default='':config.get(key,default))
    monkeypatch.setattr(policy.settings,'put',lambda key,value,**kwargs:config.update({key:value}))
    monkeypatch.setattr(policy.settings,'write_through',AsyncMock(return_value=1))
    monkeypatch.setattr(policy.users_store,'sync_user',AsyncMock(return_value=True))
    monkeypatch.setattr(nostr_dvm,'peer_pubkeys',lambda:{keys[4]})
    monkeypatch.setattr(policy.blossom_service,'_whitelist_pubkeys',lambda db:set(keys))
    users=[]
    for i,key in enumerate(keys[:4]):
        u=User(username='user'+str(i),password_hash='unused',nostr_npub=ns.npub_of(key),can_ai=True,can_blossom=True,
               is_admin=i==3,pleroma_acct='person@fedi.test' if i==1 else None)
        db.add(u);users.append(u)
    db.add(FediPuppet(actor_uri='https://fedi.test/user',acct='puppet@fedi.test',pubkey_hex=keys[5],nip05_name='puppet'))
    db.commit()
    yield SimpleNamespace(db=db,users=users,keys=keys,config=config)
    db.close();engine.dispose()


def test_preview_preserves_local_fediverse_admin_and_peer(world):
    targets,keep,result=policy.plan(world.db)
    assert targets==[world.users[2]]
    assert result==dict(domain='poster.place',accounts=1,ai=1,blossom=1,whitelist=1)
    assert keep==set(world.keys)-{world.keys[2]}
    assert world.users[2].can_ai


def test_explicitly_disabling_fediverse_exemption(world):
    targets,keep,result=policy.plan(world.db,False)
    assert {u.id for u in targets}=={world.users[1].id,world.users[2].id}
    assert world.keys[5] not in keep


def test_run_persists_revocations_and_preserves_exempt_users(world):
    result=asyncio.run(policy.run(world.db))
    assert result['accounts']==1
    u=world.users[2]
    assert not u.can_ai and not u.can_blossom and u.access_revoked
    assert all(u.can_ai and u.can_blossom for u in [world.users[0],world.users[1],world.users[3]])
    assert policy.users_store.sync_user.await_count==1
    assert 'relay_access_policy_last_run' in world.config


def test_failed_authority_write_does_not_claim_or_commit_success(world,monkeypatch):
    monkeypatch.setattr(policy.users_store,'sync_user',AsyncMock(return_value=False))
    with pytest.raises(RuntimeError):asyncio.run(policy.run(world.db))
    assert world.users[2].can_ai and world.users[2].can_blossom
    assert 'relay_access_policy_last_run' not in world.config


def test_empty_registry_refuses_cleanup(world):
    world.config['nostr_relay_nip05_names']=''
    with pytest.raises(ValueError):policy.plan(world.db)


def test_policy_defaults_off_with_fediverse_exemption(world):
    assert policy.configuration()==dict(enabled=False,exempt_fediverse=True)


def test_admin_routes_require_authentication_and_save_one_policy_record(world):
    import httpx
    from fastapi import FastAPI
    from app.routers.admin import router
    from app.auth import get_admin_user
    from app.database import get_db
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_db]=lambda:world.db
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            r=await client.get('/api/admin/relay-access-policy')
            assert r.status_code in (401,403)
            app.dependency_overrides[get_admin_user]=lambda:world.users[3]
            body={'enabled':True,'exempt_fediverse':True}
            r=await client.post('/api/admin/relay-access-policy/preview',json=body)
            assert r.status_code==200 and r.json()['accounts']==1
            r=await client.put('/api/admin/relay-access-policy',json=body)
            assert r.status_code==200
            assert policy.configuration()==body
            r=await client.post('/api/admin/relay-access-policy/run',json=body)
            assert r.status_code==200 and r.json()['accounts']==1
    asyncio.run(go())
