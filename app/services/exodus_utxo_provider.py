"""Strict, bounded adapters for the native wallet's UTXO providers."""
from decimal import Decimal
import json
import re
import httpx

from app.services import exodus_chain_service as C
from app.services.exodus_send_service import SendRefused
from app.services.exodus_utxo_sign import MAX_FEE_RATE

GENESIS = {'BTC':'000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f',
           'DOGE':'1a91e3dace36e2be3bf030a65679fe821aa1d6ef92e7c9902eb318182c355691',
           'LTC':'12a765e31ffd4059bada1e25190f6e98c99d9714d334efa41a195a7e7e04bfe2',
           'BCH':'000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'}
# Public BCH mainnet transaction from the provider's own reference documentation.
# Genesis coinbase is not available through Bitcoin-family getrawtransaction RPCs.
BCH_ANCHOR='01517ff1587fa5ffe6f5eb91c99cf3f2d22330cd7ee847e928ce90ca95bf781b'
MAX_RESPONSE=4_000_000


def _uint(value):
    if type(value) is not int or not 0<=value<2**63:
        raise SendRefused('The network returned invalid output data')
    return value


def _hash(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value):
        raise SendRefused('The network returned an invalid transaction identifier')
    return value


async def request(client,endpoint,path,*,body=None,text=False,missing=False):
    kwargs={'content':body,'headers':{'Content-Type':'text/plain'}} if isinstance(body,str) else {'json':body} if body is not None else {}
    async with client.stream('POST' if body is not None else 'GET',endpoint.rstrip('/')+path,**kwargs) as response:
        if missing and response.status_code==404:
            return None
        response.raise_for_status()
        raw=bytearray()
        async for block in response.aiter_bytes():
            if len(raw)+len(block)>MAX_RESPONSE:
                raise SendRefused('The network response is too large to verify')
            raw.extend(block)
    try:
        return raw.decode().strip() if text else json.loads(raw)
    except (ValueError,UnicodeError) as error:
        raise SendRefused('The network returned an unreadable response') from error


class Provider:
    def __init__(self,symbol,settings):
        self.symbol=symbol;self.endpoint=C.endpoint_for(symbol,settings);self.settings=settings
        reader=C._reader_for(symbol,settings)
        self.kind='doge' if reader is C._doge_balance else 'bch' if reader is C._bch_balance else 'esplora'

    async def _request(self,client,path,**kwargs):
        if self.kind!='doge':
            return await request(client,self.endpoint,path,**kwargs)
        from app.services.exodus_doge_rate import pace
        await pace()
        return await request(client,self.endpoint,path,**kwargs)

    async def network_and_fee(self,client,*,network_only=False):
        if self.kind=='doge':
            info=await self._request(client,'')
            if not isinstance(info,dict) or info.get('name')!='DOGE.main':
                raise SendRefused('The UTXO provider does not match Dogecoin mainnet')
            if network_only: return None
            fee=Decimal(_uint(info.get('medium_fee_per_kb')))/1000
        elif self.kind=='bch':
            # A known post-fork mainnet transaction distinguishes BCH from BTC/testnet.
            anchor=await self.transaction(client,BCH_ANCHOR)
            from embit.transaction import Transaction
            if not anchor or Transaction.parse(bytes.fromhex(anchor)).txid().hex()!=BCH_ANCHOR:
                raise SendRefused('The UTXO provider could not verify BCH mainnet')
            if network_only: return None
            fee=Decimal(str(self.settings.get('exodus_fee_bch_sat_vbyte','2')))
        else:
            expected=GENESIS.get(self.symbol)
            if expected is None or await self._request(client,'/block-height/0',text=True)!=expected:
                raise SendRefused('The UTXO provider does not match the selected mainnet')
            if network_only: return None
            try:
                estimates=await self._request(client,'/fee-estimates')
            except httpx.HTTPStatusError as error:
                if self.symbol!='LTC' or error.response.status_code!=404:
                    raise
                recommended=await self._request(client,'/v1/fees/recommended')
                value=recommended.get('hourFee') if isinstance(recommended,dict) else None
                if type(value) not in (int,float):
                    raise SendRefused('The network fee estimate could not be read') from None
                estimates={'6':value}
            if not isinstance(estimates,dict):
                raise SendRefused('The network fee estimate could not be read')
            fee=Decimal(str(estimates.get('6',estimates.get('2'))))
        if not fee.is_finite() or not 0<fee<=MAX_FEE_RATE[self.symbol]:
            raise SendRefused('The network fee estimate is outside the supported range')
        return fee

    async def utxos(self,client,address):
        if self.kind=='esplora':
            rows=await self._request(client,'/address/'+address+'/utxo')
            if not isinstance(rows,list) or len(rows)>2000:
                raise SendRefused('The unspent output list is incomplete')
            if any(not isinstance(row,dict) for row in rows): return self._bad()
            return [{'txid':_hash(row.get('txid')),'index':_uint(row.get('vout')),'units':_uint(row.get('value'))}
                    for row in rows]
        if self.kind=='doge':
            data=await self._request(client,'/addrs/'+address+'?unspentOnly=true&includeScript=true&limit=2000')
            if not isinstance(data,dict) or data.get('address')!=address or data.get('hasMore'):
                return self._bad()
            confirmed,unconfirmed=data.get('txrefs',[]),data.get('unconfirmed_txrefs',[])
            if not isinstance(confirmed,list) or not isinstance(unconfirmed,list): return self._bad()
            rows=confirmed+unconfirmed
            if not isinstance(rows,list) or len(rows)>2000 or any(not isinstance(row,dict) for row in rows):
                return self._bad()
            return [{'txid':_hash(row.get('tx_hash')),'index':_uint(row.get('tx_output_n')),'units':_uint(row.get('value'))}
                    for row in rows]
        data=await self._request(client,'/bch/utxos',body={'address':address})
        # Consumer service versions have returned a direct row and a one-row array.
        if isinstance(data,list) and len(data)==1:
            data=data[0]
        if (not isinstance(data,dict) or data.get('address')!=address or not isinstance(data.get('bchUtxos'),list)
                or data.get('success') is False or data.get('status',200)!=200):
            return self._bad()
        rows=data['bchUtxos']
        if len(rows)>2000 or any(not isinstance(row,dict) for row in rows):
            return self._bad()
        # Token-bearing and unclassified UTXOs are deliberately excluded, never burned as BCH.
        return [{'txid':_hash(row.get('tx_hash')),'index':_uint(row.get('tx_pos')),'units':_uint(row.get('value'))}
                for row in rows if row.get('isValid') is False and not row.get('tokenData')]

    @staticmethod
    def _bad():
        raise SendRefused('The network returned an incomplete unspent output list')

    async def transaction(self,client,txid):
        _hash(txid)
        if self.kind=='esplora':
            value=await self._request(client,'/tx/'+txid+'/hex',text=True,missing=True)
        elif self.kind=='doge':
            value=await self._request(client,'/txs/'+txid+'?includeHex=true',missing=True)
            if value is None: return None
            if not isinstance(value,dict) or value.get('hash')!=txid:
                raise SendRefused('The funding transaction does not match the requested hash')
            value=value.get('hex')
        else:
            value=await self._request(client,'/bch/txData',body={'txids':[txid]},missing=True)
            if value is None: return None
            if isinstance(value,dict):
                if value.get('status')!=200 or value.get('success') is False:
                    raise SendRefused('The funding transaction could not be read')
                value=value.get('txData')
            if not isinstance(value,list) or len(value)!=1 or not isinstance(value[0],dict) or value[0].get('txid')!=txid:
                raise SendRefused('The funding transaction could not be read')
            value=value[0].get('hex')
        if value is None: return None
        if not isinstance(value,str) or len(value)%2 or not re.fullmatch(r'[0-9a-fA-F]+',value):
            raise SendRefused('The funding transaction contains invalid bytes')
        return value

    async def unspent(self,client,txid,index):
        if self.kind=='esplora':
            result=await self._request(client,f'/tx/{txid}/outspend/{index}')
            if not isinstance(result,dict) or type(result.get('spent')) is not bool:
                raise SendRefused('The selected output could not be verified as spendable')
            return result['spent'] is False
        if self.kind=='doge':
            result=await self._request(client,'/txs/'+txid+'?limit=2000')
            if not isinstance(result,dict) or result.get('hash')!=txid or not isinstance(result.get('outputs'),list):
                return self._bad()
            if index>=len(result['outputs']): return self._bad()
            output=result['outputs'][index]
            if not isinstance(output,dict): return self._bad()
            _uint(output.get('value'))
            if not isinstance(output.get('script'),str) or not re.fullmatch(r'[0-9a-fA-F]+',output['script']):
                return self._bad()
            if output.get('spent_by') is not None:
                _hash(output['spent_by'])
                return False
            return True
        result=await self._request(client,'/bch/utxoIsValid',body={'utxo':{'tx_hash':txid,'tx_pos':index}})
        if type(result) is bool: return result
        if isinstance(result,dict) and result.get('success') is True and result.get('status')==200 and type(result.get('isValid')) is bool:
            return result['isValid']
        raise SendRefused('The selected BCH output could not be verified as spendable')

    async def broadcast(self,client,raw):
        if self.kind=='esplora':
            return await self._request(client,'/tx',body=raw,text=True)
        if self.kind=='doge':
            result=await self._request(client,'/txs/push',body={'tx':raw})
            return (result.get('tx') or {}).get('hash') if isinstance(result,dict) and not result.get('errors') and not result.get('error') else None
        result=await self._request(client,'/bch/broadcast',body={'hex':raw})
        return result if isinstance(result,str) else result.get('txid') if isinstance(result,dict) and result.get('success') is True and result.get('status')==200 else None
