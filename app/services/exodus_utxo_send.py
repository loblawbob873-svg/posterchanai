"""Wallet-owned funding discovery, final outpoint checks and durable native broadcasts."""
import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time

from app.services import exodus_bitcoin_discovery as B, exodus_derivation as D, exodus_wallet_service as W
from app.services import exodus_send_service as S, exodus_utxo_sign as U
from app.services.exodus_utxo_provider import Provider


@contextmanager
def _slot():
    folder=Path(os.environ.get('EXODUS_TRANSFER_DIR','data/exodus-transfers')).resolve()/'utxo-slots'
    folder.mkdir(parents=True,mode=0o700,exist_ok=True);folder.chmod(0o700)
    fd=None
    try:
        for index in range(2):
            candidate=os.open(folder/str(index),os.O_RDWR|os.O_CREAT,0o600)
            try:
                fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                fd=candidate;break
            except BlockingIOError:
                os.close(candidate)
        if fd is None:
            raise S.SendRefused('Native wallet sending is busy; try again shortly')
        yield
    finally:
        if fd is not None:os.close(fd)


def scope_address(phrase,symbol,account):
    # Use the same account identity across legacy/current imports and BTC script families.
    return D.address(phrase,symbol,account=account,purpose=44,format=D.EXODUS)


def spend_addresses(user_id,doc,phrase,account,key,symbol):
    base=D.address(phrase,symbol,account=account,format=D.profile(doc))
    path=B._cache_file(B._folder(user_id,account,base),symbol)
    try:
        cache=json.loads(W.unseal(bytes.fromhex(path.read_text()),key))
        stamp=cache.get('checkedAt')
        if type(stamp) not in (int,float) or not 0<=time.time()-stamp<300 or not isinstance(cache.get('addresses'),list):
            raise ValueError('Discovery is incomplete')
    except (OSError,ValueError,W.WalletError) as error:
        raise S.SendRefused('Wait for wallet address discovery to finish before sending') from error
    change_purpose=84 if symbol=='BTC' and D.profile(doc)==D.EXODUS else 44
    records=[];seen=set();last_change=-1
    for item in cache['addresses']:
        if not isinstance(item,dict): raise S.SendRefused('Wallet address discovery is incomplete')
        purpose,change,index=item.get('purpose'),item.get('change'),item.get('index')
        address=D.address(phrase,symbol,account=account,purpose=purpose,change=change,index=index,format=D.EXODUS)
        if address!=item.get('address'):
            raise S.SendRefused('Wallet address discovery does not match the selected wallet')
        if change==1 and purpose==change_purpose:
            last_change=max(last_change,index)
        if address not in seen:
            records.append({**item,'address':address});seen.add(address)
    # Include the currently advertised receive address even if its payment arrived since scan.
    purpose=84 if symbol=='BTC' and D.profile(doc)==D.EXODUS else 44
    index=int(doc.get('addressIndex') or 0)
    current=D.address(phrase,symbol,account=account,index=index,purpose=purpose,format=D.EXODUS)
    if current not in seen:
        records.append({'address':current,'purpose':purpose,'change':0,'index':index})
    if len(records)>256 or last_change>=999:
        raise S.SendRefused('This wallet exceeds the supported send discovery range')
    change=D.address(phrase,symbol,account=account,change=1,index=last_change+1,purpose=change_purpose,format=D.EXODUS)
    return records,change,path


async def send(**kwargs):
    with _slot():
        return await _send(**kwargs)


async def _send(*,user_id,doc,phrase,account,key,symbol,to,units,settings,before_broadcast):
    records,change,cache_path=await asyncio.to_thread(spend_addresses,user_id,doc,phrase,account,key,symbol)
    U.address_script(symbol,to)
    provider=Provider(symbol,settings)
    async def prepare():
        async with S._client(S.RPC_TIMEOUT) as client:
            rate=await provider.network_and_fee(client)
            semaphore=asyncio.Semaphore(3)
            async def read(record):
                async with semaphore:
                    return record,await provider.utxos(client,record['address'])
            tasks=[asyncio.create_task(read(record)) for record in records]
            try:
                groups=await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
            def derive_coins():
                coins=[]
                for record,outputs in groups:
                    if not outputs:continue
                    if len(coins)+len(outputs)>2000:
                        raise S.SendRefused('This wallet has too many outputs for one bounded send')
                    secret=D.private_key(phrase,symbol,account=account,index=record['index'],change=record['change'],
                                         purpose=record['purpose'],format=D.EXODUS)
                    script=U.address_script(symbol,record['address']).data
                    family='p2pkh' if symbol!='BTC' or record['purpose']==44 else 'p2wpkh' if record['purpose']==84 else 'p2tr'
                    coins.extend(U.Coin(row['txid'],row['index'],row['units'],script,secret,family) for row in outputs)
                return coins
            coins=await asyncio.to_thread(derive_coins)
            result=await asyncio.to_thread(U.build,symbol,coins,to=to,units=units,change=change,fee_rate=rate)
            lookup={(coin.txid,coin.index):coin for coin in coins}
            # Verify funding transaction bytes and amounts before any broadcast checkpoint.
            for outpoint in result['inputs']:
                coin=lookup[(outpoint['txid'],outpoint['index'])]
                raw=await provider.transaction(client,coin.txid)
                if raw is None: raise S.SendRefused('A funding transaction is unavailable; nothing was submitted')
                await asyncio.to_thread(U.verify_coin,coin,bytes.fromhex(raw))
                if not await provider.unspent(client,coin.txid,coin.index):
                    raise S.SendRefused('A selected output was already spent; refresh this wallet before sending')
            return result
    try:
        result=await asyncio.wait_for(prepare(),55)
    except S.SendRefused:
        raise
    except Exception as error:
        raise S.SendRefused('The funding outputs or network fee could not be verified; nothing was submitted') from error
    await before_broadcast(result['hash'],None,utxo={'inputs':result['inputs']})
    try:
        async with S._client(S.BROADCAST_TIMEOUT) as client:
            returned=await provider.broadcast(client,result['raw'])
        if returned!=result['hash']:
            raise ValueError('Unconfirmed transaction identity')
    except Exception as error:
        raise S.SendUnsure(f"Transaction {result['hash']} may have been submitted. Check its status before another payment.") from error
    finally:
        # A stale discovery value must not present the old pre-send portfolio total.
        try:
            cache_path.unlink(missing_ok=True)
        except OSError:
            pass  # Cache cleanup cannot change the outcome of an already submitted payment.
    return {name:value for name,value in result.items() if name!='raw'}|{'pending':True}


async def status(client,endpoint,symbol,record):
    from app.services import settings_store
    settings=dict(settings_store.all_settings())
    # Use current configured provider, preserving the endpoint selected by the route.
    provider=Provider(symbol,settings)
    if provider.endpoint!=endpoint:
        raise S.SendRefused('The wallet network settings changed; retry the status lookup')
    await provider.network_and_fee(client,network_only=True)
    raw=await provider.transaction(client,record['hash'])
    if raw:
        from embit.transaction import Transaction
        digest=await asyncio.to_thread(lambda:Transaction.parse(bytes.fromhex(raw)).txid().hex())
        if digest!=record['hash']:
            raise S.SendRefused('The network transaction does not match this payment')
        return {'state':'accepted','hash':record['hash']}
    # A missing transaction can still propagate later. Never automatically resign it.
    return {'state':'unconfirmed','hash':record['hash']}
