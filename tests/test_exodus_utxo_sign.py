"""Public deterministic UTXOs; decode and independently verify every signed input."""
from dataclasses import replace
import hashlib

import pytest

from app.services import exodus_derivation as D, exodus_utxo_sign as U
from app.services.exodus_send_service import SendRefused

PHRASE='abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about'


def funding(symbol='BTC',purpose=84,index=0,units=1000000):
    from embit.transaction import Transaction,TransactionInput,TransactionOutput
    address=D.address(PHRASE,symbol,purpose=purpose,index=index)
    sc=U.address_script(symbol,address)
    previous=Transaction(vin=[TransactionInput(bytes([index+1])*32,0)],vout=[TransactionOutput(units,sc)])
    coin=U.Coin(previous.txid().hex(),0,units,sc.data,D.private_key(PHRASE,symbol,purpose=purpose,index=index),
                'p2pkh' if symbol!='BTC' or purpose==44 else 'p2wpkh' if purpose==84 else 'p2tr')
    U.verify_coin(coin,previous.serialize())
    return coin,previous.serialize()


def build(symbol='BTC',purposes=(84,),units=100000,rate=2):
    coins=[funding(symbol,purpose,index,10000000 if symbol=='DOGE' else 1000000)[0] for index,purpose in enumerate(purposes)]
    to=D.address(PHRASE,symbol,index=10)
    change=D.address(PHRASE,symbol,change=1)
    return U.build(symbol,coins,to=to,units=units,change=change,fee_rate=rate),coins,to,change


def sha256d(data): return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def verify_inputs(tx,coins,symbol):
    from embit import ec,script
    for index,coin in enumerate(coins):
        public=ec.PrivateKey(coin.private_key).get_public_key()
        if coin.family=='p2tr':
            digest=tx.sighash_taproot(index,[script.Script(c.script) for c in coins],[c.units for c in coins])
            assert public.taproot_tweak().schnorr_verify(ec.SchnorrSig.parse(tx.vin[index].witness.items[0]),digest)
        else:
            if coin.family=='p2wpkh':
                signature,pub=tx.vin[index].witness.items
                digest=tx.sighash_segwit(index,script.p2pkh(public),coin.units)
            else:
                raw=tx.vin[index].script_sig.data;length=raw[0]
                signature=raw[1:length+1];pub=raw[length+2:]
                if symbol=='BCH':
                    # Independent BIP143/FORKID-ALL preimage, including exact amounts. This
                    # catches using BTC legacy signatures or losing the 0x40 replay flag.
                    preimage=(tx.version.to_bytes(4,'little')+
                        sha256d(b''.join(bytes.fromhex(c.txid)[::-1]+c.index.to_bytes(4,'little') for c in coins))+
                        sha256d(b''.join(i.sequence.to_bytes(4,'little') for i in tx.vin))+
                        bytes.fromhex(coin.txid)[::-1]+coin.index.to_bytes(4,'little')+
                        script.Script(coin.script).serialize()+coin.units.to_bytes(8,'little')+
                        tx.vin[index].sequence.to_bytes(4,'little')+sha256d(b''.join(o.serialize() for o in tx.vout))+
                        tx.locktime.to_bytes(4,'little')+(0x41).to_bytes(4,'little'))
                    digest=sha256d(preimage)
                else:
                    digest=tx.sighash_legacy(index,script.Script(coin.script))
            assert pub==public.sec()
            assert signature[-1]==(0x41 if symbol=='BCH' else 1)
            assert public.verify(ec.Signature.parse(signature[:-1]),digest)


@pytest.mark.parametrize('symbol,purposes', [('BTC',(44,)),('BTC',(84,)),('BTC',(86,)),
    ('BTC',(44,84,86)),('LTC',(44,44)),('DOGE',(44,44)),('BCH',(44,44))])
def test_exact_recipient_change_fee_and_valid_signature_for_every_spent_family(symbol,purposes):
    from embit.transaction import Transaction
    amount=(10000000 if symbol=='DOGE' else 1000000)*(len(purposes)-1)+(1000000 if symbol=='DOGE' else 100000)
    result,coins,to,change=build(symbol,purposes,units=amount)
    tx=Transaction.parse(bytes.fromhex(result['raw']))
    lookup={(c.txid,c.index):c for c in coins}
    ordered=[lookup[(inp.txid.hex(),inp.vout)] for inp in tx.vin]
    verify_inputs(tx,ordered,symbol)
    assert tx.txid().hex()==result['hash']
    assert tx.vout[0].value==result['units'] and tx.vout[0].script_pubkey==U.address_script(symbol,to)
    assert tx.vout[1].value==result['change'] and tx.vout[1].script_pubkey==U.address_script(symbol,change)
    assert sum(c.units for c in ordered)==sum(o.value for o in tx.vout)+result['fee']
    assert result['fee']>=result['vbytes']*2


@pytest.mark.parametrize('field,value',[('units',999999),('index',1),('txid','aa'*32),('script',b'bad'),('private_key',b'\x02'*32)])
def test_provider_cannot_substitute_funding_amount_script_outpoint_or_key(field,value):
    coin,raw=funding()
    with pytest.raises(SendRefused): U.verify_coin(replace(coin,**{field:value}),raw)


@pytest.mark.parametrize('symbol,wrong',[('BTC','LTC'),('LTC','DOGE'),('DOGE','BTC'),('BCH','BTC')])
def test_cross_network_addresses_are_rejected(symbol,wrong):
    with pytest.raises(SendRefused): U.address_script(symbol,D.address(PHRASE,wrong))


@pytest.mark.parametrize('rate',['NaN','Infinity',0,-1,10001,'bad'])
def test_invalid_fee_rates_never_produce_a_transaction(rate):
    with pytest.raises(SendRefused): build(rate=rate)


def test_duplicate_outpoint_never_creates_a_double_input():
    coin,_=funding()
    with pytest.raises(SendRefused,match='duplicate'):
        U.build('BTC',[coin,coin],to=D.address(PHRASE,'BTC'),units=10000,change=D.address(PHRASE,'BTC',change=1),fee_rate=2)


def test_insufficient_funds_and_dust_payment_are_refused():
    with pytest.raises(SendRefused,match='Not enough'): build(units=1000000)
    with pytest.raises(SendRefused,match='minimum'): build(units=1)


def test_small_change_is_added_to_fee_and_never_becomes_a_dust_output():
    from embit.transaction import Transaction
    result,_,_,_=build(units=999500,rate=2)
    assert len(Transaction.parse(bytes.fromhex(result['raw'])).vout)==1
    assert result['change']==0 and result['fee']==500


def test_fee_ceiling_is_enforced_on_the_signed_amount():
    coin,_=funding(units=100000000)
    with pytest.raises(SendRefused,match='fee exceeds'):
        U.build('BTC',[coin],to=D.address(PHRASE,'BTC'),units=1000000,change=D.address(PHRASE,'BTC',change=1),fee_rate=10000)


def test_input_count_is_bounded_even_if_more_outputs_could_cover_the_payment():
    coin,_=funding(units=10000)
    coins=[replace(coin,txid=f'{index+1:064x}') for index in range(U.MAX_INPUTS+1)]
    with pytest.raises(SendRefused,match='Not enough'):
        U.build('BTC',coins,to=D.address(PHRASE,'BTC'),units=505000,change=D.address(PHRASE,'BTC',change=1),fee_rate=1)
