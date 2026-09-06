"""Bounded receive/change discovery for Bitcoin, Litecoin, Dogecoin and Bitcoin Cash."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import time

from app.services import exodus_derivation as D, exodus_chain_service as C, exodus_wallet_service as W
from app.services.exodus_monero import _write
from app.services.exodus_transfers import _lock

GAP = 20
MAX_INDEX = 1000
SYMBOLS = ('BTC', 'LTC', 'DOGE', 'BCH')
_JOBS = {}


class Incomplete(W.WalletError):
    pass


async def address_state(client, endpoint, address):
    response = await client.get(endpoint.rstrip('/') + '/address/' + address)
    response.raise_for_status()
    data = response.json()
    parts = [data.get('chain_stats'), data.get('mempool_stats')]
    if any(not isinstance(part, dict) for part in parts):
        raise Incomplete('Bitcoin address history could not be read')
    values = [part.get(key) for part in parts for key in ('funded_txo_sum','spent_txo_sum','tx_count')]
    if any(type(value) is not int or value < 0 for value in values):
        raise Incomplete('Bitcoin address history is incomplete')
    units = values[0] + values[3] - values[1] - values[4]
    if units < 0:
        raise Incomplete('Bitcoin balance is inconsistent')
    used = values[2] + values[5] > 0
    if any(values[index] for index in (0, 1, 3, 4)) and not used:
        raise Incomplete('Address balance and transaction history disagree')
    return {'units':units, 'used':used}


async def other_address_state(client, endpoint, address, symbol, settings=None):
    reader = C._reader_for(symbol, settings)
    if reader is C._utxo_balance:
        return await address_state(client, endpoint, address)
    if reader is C._doge_balance:
        response = await client.get(endpoint.rstrip('/') + '/addrs/' + address + '/balance')
        response.raise_for_status()
        data = response.json()
        units, count = data.get('final_balance'), data.get('final_n_tx')
        if data.get('address') != address or type(units) is not int or units < 0 or type(count) is not int or count < 0:
            raise Incomplete('Dogecoin address history is incomplete')
        state = {'units': units, 'used': count > 0}
    elif reader is C._bch_balance:
        units = await C._bch_balance(client, endpoint, address, symbol)
        response = await client.post(endpoint.rstrip('/') + '/bch/txHistory',
                                     json={'address': address, 'sortOrder': 'DESCENDING'})
        response.raise_for_status()
        data = response.json()
        transactions = data.get('txs')
        if units is None or data.get('success') is not True or data.get('address') != address or not isinstance(transactions, list):
            raise Incomplete('Bitcoin Cash address history is incomplete')
        # Only existence of history matters, so a non-empty first page is sufficient. A spent
        # address must continue the discovery branch even when its current balance is zero.
        if any(not isinstance(tx, dict) or not isinstance(tx.get('tx_hash'), str) or not re.fullmatch(r'[0-9a-f]{64}', tx['tx_hash']) for tx in transactions):
            raise Incomplete('Bitcoin Cash transaction history is unreadable')
        state = {'units': units, 'used': bool(transactions)}
    else:
        raise Incomplete('This asset has no address history provider')
    if state['units'] and not state['used']:
        raise Incomplete('Address balance and transaction history disagree')
    return state


async def scan(doc, phrase, account, endpoint, *, gap=GAP, maximum=MAX_INDEX, symbol='BTC', settings=None):
    """A spent address still counts as used; one failed lookup invalidates the total."""
    if symbol not in SYMBOLS:
        raise Incomplete('Unsupported discovery asset')
    paths = [(purpose, change) for purpose in ((44,84,86) if symbol == 'BTC' else (44,)) for change in (0,1)]
    semaphore = asyncio.Semaphore(3)
    doge_lock = asyncio.Lock()
    async with C._client() as client:
        async def branch(purpose, change):
            unused, index, found, units = 0, 0, [], 0
            while unused < gap:
                if index >= maximum:
                    raise Incomplete(f'{symbol} address discovery reached its limit')
                async with semaphore:
                    address = await asyncio.to_thread(D.address, phrase, symbol, account=account, purpose=purpose,
                                                       change=change, index=index, format=D.EXODUS)
                    if symbol == 'BTC':
                        state = await address_state(client, endpoint, address)
                    elif symbol == 'DOGE' and C._reader_for(symbol, settings) is C._doge_balance:
                        # Stay below the default provider's per-second budget for one scan.
                        async with doge_lock:
                            state = await other_address_state(client, endpoint, address, symbol, settings)
                            await asyncio.sleep(.5)
                    else:
                        state = await other_address_state(client, endpoint, address, symbol, settings)
                unused = 0 if state['used'] else unused + 1
                units += state['units']
                if state['used']:
                    found.append({'address':address, 'purpose':purpose,'change':change,'index':index})
                index += 1
            return {'units':units,'addresses':found,'checked':index}
        tasks = [asyncio.create_task(branch(*path)) for path in paths]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return {'units':sum(result['units'] for result in results),
            'addresses':[address for result in results for address in result['addresses']],
            'checkedAt':time.time(), 'gap':gap}


def _folder(user_id, account, address):
    identity = hashlib.sha256(json.dumps([user_id,account,address]).encode()).hexdigest()
    root = Path(os.environ.get('EXODUS_DISCOVERY_DIR','data/exodus-discovery')).resolve()
    folder = root / identity
    folder.mkdir(parents=True,mode=0o700,exist_ok=True)
    root.chmod(0o700);folder.chmod(0o700)
    return folder


def _cache_file(folder, symbol):
    return folder / ('bitcoin.enc' if symbol == 'BTC' else symbol.lower() + '.enc')


async def _refresh(folder, doc, phrase, account, key, endpoint, symbol='BTC', settings=None):
    try:
        with _lock(folder):
            result = await asyncio.wait_for(scan(doc,phrase,account,endpoint,symbol=symbol,settings=settings), timeout=240)
            _write(_cache_file(folder,symbol),W.seal(json.dumps(result),key).hex())
    except Exception:
        return False
    return True


async def balance(user_id, doc, phrase, account, key, settings, symbol='BTC'):
    if symbol not in SYMBOLS:
        raise Incomplete('Unsupported discovery asset')
    address = D.address(phrase,symbol,account=account,format=D.profile(doc))
    folder = _folder(user_id,account,address)
    cache = None
    try:
        cache = json.loads(W.unseal(bytes.fromhex(_cache_file(folder,symbol).read_text()),key))
    except (OSError,ValueError,W.WalletError):
        pass
    stamp = cache.get('checkedAt') if isinstance(cache,dict) else None
    age = time.time()-stamp if type(stamp) in (int,float) else float('inf')
    known = bool(cache and type(cache.get('units')) is int and cache['units'] >= 0 and 0 <= age < 300)
    if age >= (60 if symbol == 'BTC' else 240):
        for name,task in list(_JOBS.items()):
            if task.done(): _JOBS.pop(name,None)
        if str(folder) not in _JOBS and len(_JOBS) < 4:
            _JOBS[str(folder)] = asyncio.create_task(_refresh(folder,doc,phrase,account,key,C.endpoint_for(symbol,settings),symbol,settings))
    return {'address':address,'known':known,'units':cache['units'] if known else None,
            'amount':W.from_base_units(cache['units'],symbol) if known else None,
            'checkedAt':stamp if known else None,
            'note':'' if known else f'Discovering {symbol} receive and change addresses'}
