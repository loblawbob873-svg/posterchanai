"""Find imported Bitcoin funds across receive/change and legacy/SegWit/Taproot paths."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

from app.services import exodus_derivation as D, exodus_chain_service as C, exodus_wallet_service as W
from app.services.exodus_monero import _write
from app.services.exodus_transfers import _lock

GAP = 20
MAX_INDEX = 1000
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
    return {'units':units, 'used':values[2] + values[5] > 0}


async def scan(doc, phrase, account, endpoint, *, gap=GAP, maximum=MAX_INDEX):
    """A spent address still counts as used; one failed lookup invalidates the total."""
    paths = [(purpose, change) for purpose in (44,84,86) for change in (0,1)]
    semaphore = asyncio.Semaphore(3)
    async with C._client() as client:
        async def branch(purpose, change):
            unused, index, found, units = 0, 0, [], 0
            while unused < gap:
                if index >= maximum:
                    raise Incomplete('Bitcoin address discovery reached its limit')
                async with semaphore:
                    address = await asyncio.to_thread(D.address, phrase, 'BTC', account=account, purpose=purpose,
                                                       change=change, index=index, format=D.EXODUS)
                    state = await address_state(client, endpoint, address)
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


async def _refresh(folder, doc, phrase, account, key, endpoint):
    try:
        with _lock(folder):
            result = await asyncio.wait_for(scan(doc,phrase,account,endpoint), timeout=240)
            _write(folder/'bitcoin.enc',W.seal(json.dumps(result),key).hex())
    except Exception:
        return False
    return True


async def balance(user_id, doc, phrase, account, key, settings):
    address = D.address(phrase,'BTC',account=account,format=D.profile(doc))
    folder = _folder(user_id,account,address)
    cache = None
    try:
        cache = json.loads(W.unseal(bytes.fromhex((folder/'bitcoin.enc').read_text()),key))
    except (OSError,ValueError,W.WalletError):
        pass
    stamp = cache.get('checkedAt') if isinstance(cache,dict) else None
    age = time.time()-stamp if type(stamp) in (int,float) else float('inf')
    known = bool(cache and type(cache.get('units')) is int and cache['units'] >= 0 and 0 <= age < 300)
    if age >= 60:
        for name,task in list(_JOBS.items()):
            if task.done(): _JOBS.pop(name,None)
        if str(folder) not in _JOBS and len(_JOBS) < 4:
            _JOBS[str(folder)] = asyncio.create_task(_refresh(folder,doc,phrase,account,key,C.endpoint_for('BTC',settings)))
    return {'address':address,'known':known,'units':cache['units'] if known else None,
            'amount':W.from_base_units(cache['units'],'BTC') if known else None,
            'checkedAt':stamp if known else None,
            'note':'' if known else 'Discovering Bitcoin receive and change addresses'}
