import pytest
from app.services import exodus_bitcoin_discovery as B, exodus_derivation as D

PHRASE='abandon '*11+'about'

@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.mark.anyio
async def test_change_segwit_taproot_and_spent_history_all_contribute(monkeypatch):
    fixtures={}
    for purpose,change,index,units in [(44,0,0,0),(44,0,2,10),(84,1,1,20),(86,0,1,30)]:
        address=D.address(PHRASE,'BTC',purpose=purpose,change=change,index=index)
        fixtures[address]={'units':units,'used':True}
    seen=[]
    async def state(client,endpoint,address):
        seen.append(address)
        return fixtures.get(address,{'units':0,'used':False})
    monkeypatch.setattr(B,'address_state',state)
    got=await B.scan({'derivation':D.EXODUS},PHRASE,0,'https://fixture.invalid',gap=2,maximum=20)
    assert got['units']==60
    assert {row['address'] for row in got['addresses']}==set(fixtures)
    assert all(address in seen for address in fixtures)


@pytest.mark.anyio
async def test_one_unreadable_branch_cannot_be_persisted_as_a_complete_balance(monkeypatch):
    failing=D.address(PHRASE,'BTC',purpose=86,change=1)
    async def state(client,endpoint,address):
        if address==failing: raise B.Incomplete('missing response')
        return {'units':0,'used':False}
    monkeypatch.setattr(B,'address_state',state)
    with pytest.raises(B.Incomplete):
        await B.scan({},PHRASE,0,'https://fixture.invalid',gap=2)


@pytest.mark.anyio
async def test_discovery_limit_is_unknown_instead_of_truncating_imported_funds(monkeypatch):
    async def state(*args): return {'units':1,'used':True}
    monkeypatch.setattr(B,'address_state',state)
    with pytest.raises(B.Incomplete,match='limit'):
        await B.scan({},PHRASE,0,'https://fixture.invalid',gap=2,maximum=3)
