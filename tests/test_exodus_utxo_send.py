import json
from pathlib import Path
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import exodus_utxo_send as U, exodus_utxo_provider as P, exodus_transfers as T
from app.services import exodus_send_service as S, exodus_bitcoin_discovery as B, exodus_derivation as D, exodus_wallet_service as W
from tests.test_exodus_utxo_sign import funding,PHRASE,verify_inputs

REFERENCE=json.loads((Path(__file__).parent/'fixtures/exodus/bch-provider-reference.json').read_text())
KEY=b'z'*32
TOKEN='12'*16


@pytest.mark.anyio
async def test_actual_dogecoin_mainnet_fee_quote_is_preserved_in_atomic_units():
    from decimal import Decimal
    payload={'name':'DOGE.main','medium_fee_per_kb':54470068,
             'high_fee_per_kb':259827356,'low_fee_per_kb':7146037}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=payload))) as client:
        assert await P.Provider('DOGE',{}).network_and_fee(client)==Decimal('54470.068')


@pytest.mark.anyio
@pytest.mark.parametrize('value', [1,0.5,None,True,'1',-1,0,float('inf'),float('nan'),10001])
async def test_litecoin_actual_recommended_endpoint_validates_fee(value):
    calls=[]
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/block-height/0'):return httpx.Response(200,text=P.GENESIS['LTC'])
        if request.url.path.endswith('/fee-estimates'):return httpx.Response(404,text='endpoint does not exist')
        return httpx.Response(200,text=json.dumps({'fastestFee':1,'halfHourFee':1,'hourFee':value,'economyFee':1,'minimumFee':1}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider=P.Provider('LTC',{})
        if type(value) in (int,float) and value in (1,0.5):
            assert await provider.network_and_fee(client)==value
        else:
            with pytest.raises(S.SendRefused):await provider.network_and_fee(client)
    assert calls[-1].endswith('/v1/fees/recommended')


@pytest.mark.anyio
@pytest.mark.parametrize('symbol,status',[('LTC',429),('LTC',500),('LTC',403),('BTC',404)])
async def test_fee_fallback_never_masks_other_chain_or_provider_errors(symbol,status):
    calls=[]
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/block-height/0'):return httpx.Response(200,text=P.GENESIS[symbol])
        return httpx.Response(status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.HTTPStatusError):await P.Provider(symbol,{}).network_and_fee(client)
    assert len(calls)==2


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.fixture(autouse=True)
def folders(tmp_path,monkeypatch):
    monkeypatch.setenv('EXODUS_TRANSFER_DIR',str(tmp_path/'transfers'))
    monkeypatch.setenv('EXODUS_DISCOVERY_DIR',str(tmp_path/'discovery'))
    monkeypatch.setattr('app.services.exodus_doge_rate.pace',AsyncMock())


def fixture(monkeypatch,tmp_path,symbol='BTC',failure=None):
    from embit.transaction import Transaction
    units=100000000 if symbol=='DOGE' else 1000000
    coin,raw=funding(symbol,84 if symbol=='BTC' else 44,units=units)
    address=D.address(PHRASE,symbol)
    change=D.address(PHRASE,symbol,change=1)
    config={f'exodus_rpc_{symbol.lower()}':'https://fixture.invalid',
            f'exodus_api_{symbol.lower()}':'blockcypher' if symbol=='DOGE' else 'fullstack' if symbol=='BCH' else 'esplora'}
    cache=tmp_path/'balance.enc';cache.write_text('pre-send-cache')
    monkeypatch.setattr(U,'spend_addresses',lambda *args:([
        {'address':address,'purpose':84 if symbol=='BTC' else 44,'change':0,'index':0}],change,cache))
    monkeypatch.setattr('app.services.settings_store.all_settings',lambda:config)
    calls=[];sent=[];checkpoint=[]
    def handle(request):
        path=request.url.path;body=json.loads(request.content) if request.content and request.headers.get('Content-Type')!='text/plain' else None
        calls.append(path)
        if path in ('/tx','/txs/push','/bch/broadcast'):
            encoded=request.content.decode() if path=='/tx' else body['tx'] if path=='/txs/push' else body['hex']
            tx=Transaction.parse(bytes.fromhex(encoded));sent.append(tx)
            verify_inputs(tx,[coin],symbol)
            assert checkpoint[0]==tx.txid().hex()
            journal=T._read(T._folder(identity)/(TOKEN+'.enc'),KEY)
            assert journal['state']=='broadcast' and journal['hash']==checkpoint[0]
            assert journal['utxo']['inputs']==[{'txid':coin.txid,'index':coin.index}]
            if failure=='lost-ack':raise httpx.ReadTimeout('lost reply',request=request)
            txid=tx.txid().hex()
            return httpx.Response(200,text=txid) if symbol in ('BTC','LTC') else httpx.Response(200,json={'tx':{'hash':txid}} if symbol=='DOGE' else {'success':True,'status':200,'txid':txid})
        if path=='/block-height/0':return httpx.Response(200,text='wrong-chain' if failure=='network' else P.GENESIS[symbol])
        if path=='/fee-estimates':return httpx.Response(200,json={'6':2})
        # Actual BlockCypher mainnet quote: atomic DOGE units per kilobyte,
        # substantially larger than Bitcoin's atomic-unit fee rates.
        if path=='/':return httpx.Response(200,json={'name':'wrong-chain' if failure=='network' else 'DOGE.main',
            'medium_fee_per_kb':54470068,'high_fee_per_kb':259827356,'low_fee_per_kb':7146037})
        if '/address/' in path or '/addrs/' in path or path=='/bch/utxos':
            amount=coin.units+1 if failure=='amount' else coin.units
            if symbol in ('BTC','LTC'):value=[{'txid':coin.txid,'vout':0,'value':amount}]
            elif symbol=='DOGE':value={'address':address,'txrefs':[{'tx_hash':coin.txid,'tx_output_n':0,'value':amount}]}
            else:value=[{'address':address,'status':200,'bchUtxos':[{'tx_hash':coin.txid,'tx_pos':0,'value':amount,'isValid':False}]}]
            return httpx.Response(200,json=value)
        if '/outspend/' in path:return httpx.Response(200,json={'spent':failure=='spent'})
        if path=='/bch/utxoIsValid':return httpx.Response(200,json={'success':True,'status':200,'isValid':failure!='spent'})
        txid=body['txids'][0] if symbol=='BCH' else path.split('/')[2]
        if symbol=='BCH' and txid==P.BCH_ANCHOR:
            return httpx.Response(200,json={'status':200,'txData':[dict(REFERENCE,hex=raw.hex()) if failure=='network' else REFERENCE]})
        if symbol=='DOGE' and 'includeHex' not in request.url.params:
            assert request.url.params.get('limit')=='2000'
            return httpx.Response(200,json={'hash':coin.txid,'outputs':[{'value':coin.units,'script':coin.script.hex(),'spent_by':'ab'*32 if failure=='spent' else None}]})
        transaction=raw.hex() if txid==coin.txid else sent[-1].serialize().hex() if sent and txid==sent[-1].txid().hex() else None
        if transaction is None:return httpx.Response(404)
        if failure=='funding-bytes' and txid==coin.txid:transaction=raw[:-1].hex()+'01'
        if symbol in ('BTC','LTC'):return httpx.Response(200,text=transaction)
        if symbol=='DOGE':return httpx.Response(200,json={'hash':txid,'hex':transaction})
        return httpx.Response(200,json={'status':200,'txData':[{'txid':txid,'hex':transaction}]})
    monkeypatch.setattr(S,'_client',lambda timeout:httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    async def operation(before):
        async def capture(txid,nonce,**metadata):
            checkpoint.append(txid);await before(txid,nonce,**metadata)
        return await U.send(user_id=1,doc={'derivation':D.EXODUS},phrase=PHRASE,account=0,key=KEY,
                    symbol=symbol,to=D.address(PHRASE,symbol,index=1),units=10000000 if symbol=='DOGE' else 100000,
                    settings=config,before_broadcast=capture)
    identity=T.scope(1,symbol,U.scope_address(PHRASE,symbol,0))
    async def send(token=TOKEN):
        return await T.send(identity,KEY,token,D.address(PHRASE,symbol,index=1),1,operation,symbol=symbol)
    return send,calls,sent,checkpoint,cache,identity,config


@pytest.mark.anyio
@pytest.mark.parametrize('symbol',['BTC','LTC','DOGE','BCH'])
async def test_native_provider_flow_checkpoints_real_signature_and_blocks_duplicate_until_status(tmp_path,monkeypatch,symbol):
    send,calls,sent,checkpoint,cache,identity,config=fixture(monkeypatch,tmp_path,symbol)
    result=await send()
    assert result['pending'] and len(sent)==1 and not cache.exists()
    assert await send()==result
    with pytest.raises(S.SendUnsure):await send('34'*16)
    status=await T.status(identity,KEY,symbol,config[f'exodus_rpc_{symbol.lower()}'])
    assert status['state']=='accepted' and status['hash']==checkpoint[0]
    assert len(sent)==1


@pytest.mark.anyio
@pytest.mark.parametrize('symbol',['BTC','LTC','DOGE','BCH'])
@pytest.mark.parametrize('failure',['amount','funding-bytes','spent'])
async def test_bad_funding_or_spent_output_never_reaches_broadcast(tmp_path,monkeypatch,symbol,failure):
    send,calls,sent,checkpoint,cache,_,_=fixture(monkeypatch,tmp_path,symbol,failure)
    with pytest.raises(S.SendRefused):await send()
    assert not sent and not checkpoint and cache.exists()


@pytest.mark.anyio
@pytest.mark.parametrize('symbol',['BTC','LTC','DOGE','BCH'])
async def test_lost_ack_blocks_both_old_and_new_requests(tmp_path,monkeypatch,symbol):
    send,calls,sent,checkpoint,cache,_,_=fixture(monkeypatch,tmp_path,symbol,'lost-ack')
    for token in (TOKEN,TOKEN,'34'*16):
        with pytest.raises(S.SendUnsure):await send(token)
    assert len(sent)==1 and not cache.exists()


def test_spend_discovery_requires_fresh_wallet_owned_paths(tmp_path,monkeypatch):
    doc={'derivation':D.EXODUS};address=D.address(PHRASE,'BTC')
    path=B._cache_file(B._folder(1,0,address),'BTC')
    with pytest.raises(S.SendRefused,match='discovery'):U.spend_addresses(1,doc,PHRASE,0,KEY,'BTC')
    def write(stamp,addr):
        path.write_text(W.seal(json.dumps({'checkedAt':stamp,'addresses':[
            {'address':addr,'purpose':84,'change':1,'index':3}]}),KEY).hex())
    write(time.time()-301,D.address(PHRASE,'BTC',change=1,index=3))
    with pytest.raises(S.SendRefused):U.spend_addresses(1,doc,PHRASE,0,KEY,'BTC')
    write(time.time(),'other-wallet-address')
    with pytest.raises(S.SendRefused,match='does not match'):U.spend_addresses(1,doc,PHRASE,0,KEY,'BTC')
    write(time.time(),D.address(PHRASE,'BTC',change=1,index=3))
    records,change,_=U.spend_addresses(1,doc,PHRASE,0,KEY,'BTC')
    assert change==D.address(PHRASE,'BTC',change=1,index=4)
    assert {record['change'] for record in records}=={0,1}


def test_legacy_bitcoin_change_retains_the_original_bip44_family():
    doc={'derivation':D.LEGACY};address=D.address(PHRASE,'BTC',format=D.LEGACY)
    path=B._cache_file(B._folder(1,0,address),'BTC')
    previous=D.address(PHRASE,'BTC',purpose=44,change=1,index=2)
    path.write_text(W.seal(json.dumps({'checkedAt':time.time(),'addresses':[
        {'address':previous,'purpose':44,'change':1,'index':2}]}),KEY).hex())
    _,change,_=U.spend_addresses(1,doc,PHRASE,0,KEY,'BTC')
    assert change==D.address(PHRASE,'BTC',purpose=44,change=1,index=3)


@pytest.mark.anyio
@pytest.mark.parametrize('symbol',['BTC','LTC','DOGE','BCH'])
async def test_wrong_network_refuses_before_signing_or_checkpoint(tmp_path,monkeypatch,symbol):
    send,_,sent,checkpoint,_,_,_=fixture(monkeypatch,tmp_path,symbol,'network')
    with pytest.raises(S.SendRefused):await send()
    assert not sent and not checkpoint


@pytest.mark.anyio
async def test_cache_cleanup_error_cannot_report_a_submitted_payment_as_failed(tmp_path,monkeypatch):
    send,_,sent,_,cache,_,_=fixture(monkeypatch,tmp_path)
    original=Path.unlink
    def unlink(path,*args,**kwargs):
        if path==cache:raise OSError('simulated cache permission error')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',unlink)
    first=await send()
    assert first['pending'] and len(sent)==1
    assert await send()==first


def test_node_wide_slots_bound_native_send_work_and_release_after_errors():
    with U._slot(),U._slot():
        with pytest.raises(S.SendRefused,match='busy'):
            with U._slot():raise AssertionError('third send started')
    with U._slot():pass


@pytest.mark.anyio
async def test_bch_native_outputs_never_include_tokens_or_unknown_classification():
    address=D.address(PHRASE,'BCH');provider=P.Provider('BCH',{})
    base={'tx_hash':'ab'*32,'tx_pos':0,'value':1000,'isValid':False}
    data=[{'address':address,'status':200,'bchUtxos':[base,dict(base,tx_pos=1,isValid=True),
          dict(base,tx_pos=2,isValid=None),dict(base,tx_pos=3,tokenData={'category':'cd'*32})],
          'nullUtxos':[dict(base,tx_pos=4)]}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=data))) as client:
        assert await provider.utxos(client,address)==[{'txid':'ab'*32,'index':0,'units':1000}]


@pytest.mark.anyio
@pytest.mark.parametrize('bad,include_address',[({'address':'wrong','bchUtxos':[]},True),
    ({'address':None,'bchUtxos':[]},True),({'bchUtxos':[]},False),({'bchUtxos':None},True),
    ({'success':False,'status':422,'bchUtxos':[]},True),
    ({'bchUtxos':[{'isValid':False,'tx_hash':'bad','tx_pos':0,'value':1}]},True)])
async def test_invalid_bch_provider_envelopes_never_become_spendable_outputs(bad,include_address):
    address=D.address(PHRASE,'BCH');provider=P.Provider('BCH',{})
    response={**({'address':address} if include_address else {}),**bad}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=[response]))) as client:
        with pytest.raises(S.SendRefused):await provider.utxos(client,address)


@pytest.mark.anyio
async def test_valid_empty_bch_envelope_remains_an_empty_output_list():
    address=D.address(PHRASE,'BCH');provider=P.Provider('BCH',{})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=[{'address':address,'bchUtxos':[]}])) ) as client:
        assert await provider.utxos(client,address)==[]


@pytest.mark.anyio
@pytest.mark.parametrize('output',[{}, {'value':True,'script':'76a9'}, {'value':1,'script':'nothex'},
                                  {'value':1,'script':'76a9','spent_by':'invalid-hash'}])
async def test_dogecoin_missing_outpoint_fields_are_not_treated_as_unspent(output):
    provider=P.Provider('DOGE',{})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'hash':'ab'*32,'outputs':[output]}))) as client:
        with pytest.raises(S.SendRefused):await provider.unspent(client,'ab'*32,0)


@pytest.mark.anyio
@pytest.mark.parametrize('symbol,response',[('DOGE',{'error':'rejected','tx':{'hash':'ab'*32}}),
                                         ('BCH',{'success':False,'status':422,'txid':'ab'*32})])
async def test_explicit_broadcast_error_never_counts_as_acknowledgement(symbol,response):
    provider=P.Provider(symbol,{})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=response))) as client:
        assert await provider.broadcast(client,'public-signed-fixture') is None


@pytest.mark.anyio
async def test_provider_response_size_is_bounded_before_decode(monkeypatch):
    monkeypatch.setattr(P,'MAX_RESPONSE',10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=b'x'*11))) as client:
        with pytest.raises(S.SendRefused,match='too large'):await P.request(client,'https://fixture.invalid','/tx',text=True)


@pytest.mark.anyio
async def test_slow_key_derivation_does_not_block_signer_event_loop(tmp_path,monkeypatch):
    import asyncio
    import threading
    send,_,_,_,_,_,_=fixture(monkeypatch,tmp_path)
    deriving=threading.Event();beats=[]
    original=D.private_key
    def slow(*args,**kwargs):
        deriving.set()
        try:
            time.sleep(.1)
            return original(*args,**kwargs)
        finally:deriving.clear()
    monkeypatch.setattr(D,'private_key',slow)
    task=asyncio.create_task(send())
    while not task.done():
        if deriving.is_set():beats.append(1)
        await asyncio.sleep(.002)
    await task
    assert len(beats)>=10,'wallet derivation blocked the shared application event loop'
