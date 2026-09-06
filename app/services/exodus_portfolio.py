"""Price-backed portfolio values and encrypted observed history; no synthetic performance."""
import asyncio
from decimal import Decimal, InvalidOperation, localcontext
import time

import httpx

from app.services import exodus_vault, nostr_store

COINS = {'BTC': 'bitcoin', 'ETH': 'ethereum', 'LTC': 'litecoin', 'DOGE': 'dogecoin',
         'BCH': 'bitcoin-cash', 'MATIC': 'polygon-ecosystem-token', 'BNB': 'binancecoin',
         'AVAX': 'avalanche-2', 'SOL': 'solana', 'XRP': 'ripple', 'XMR': 'monero'}
_cache = {}
_attempted = 0.0
_price_lock = asyncio.Lock()
_history_locks = {}


def number(value):
    if isinstance(value, bool) or value is None or len(str(value)) > 100:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 <= result <= Decimal('1e30') else None
    except (InvalidOperation, ValueError):
        return None


async def prices():
    global _cache, _attempted
    async with _price_lock:
        if time.time() - _attempted >= 90:
            _attempted = time.time()
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    response = await client.get('https://api.coingecko.com/api/v3/simple/price', params={
                        'ids': ','.join(COINS.values()), 'vs_currencies': 'usd',
                        'include_24hr_change': 'true', 'include_last_updated_at': 'true'})
                    response.raise_for_status()
                    data = response.json()
                fresh = {}
                for symbol, coin in COINS.items():
                    row = data.get(coin) or {}
                    quote = number(row.get('usd'))
                    stamp = number(row.get('last_updated_at'))
                    if quote is not None and quote > 0 and stamp and stamp <= time.time() + 60:
                        fresh[symbol] = {'usd': str(quote), 'at': int(stamp)}
                if fresh:
                    _cache = fresh
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                pass
    return dict(_cache)


def value(balances, quotes, now=None):
    now = time.time() if now is None else now
    missing, assets, stamps = [], {}, []
    total = Decimal(0)
    with localcontext() as context:
        context.prec = 80
        for symbol, balance in balances.items():
            amount = number(balance.get('amount')) if balance.get('known') is True else None
            row = quotes.get(symbol) or {}
            price, stamp = number(row.get('usd')), number(row.get('at'))
            stale = bool(stamp is None or now - float(stamp) > 900 or float(stamp) > now + 60)
            known = amount is not None and (amount == 0 or (price is not None and price > 0 and not stale))
            usd = amount * price if known and amount else Decimal(0) if known else None
            assets[symbol] = {'usd': str(usd.quantize(Decimal('.01'))) if usd is not None else None,
                              'price': str(price) if price is not None else None, 'stale': stale}
            if usd is None:
                missing.append(symbol)
            else:
                total += usd
                if amount and stamp:
                    stamps.append(int(stamp))
        known_total = str(total.quantize(Decimal('.01')))
    return {'currency': 'USD', 'complete': not missing, 'total': known_total if not missing else None,
            'known_total': known_total, 'missing': missing, 'assets': assets,
            'prices_at': min(stamps) if stamps else None}


async def history(db, user, wallet_id, portfolio, valuation):
    """Record at most one complete observation per 15 minutes, scoped to the selected portfolio."""
    tag = f'pcai:exodus:history:{wallet_id}:{portfolio}'
    lock = _history_locks.setdefault((user.id, wallet_id, portfolio), asyncio.Lock())
    async with lock:
        key = nostr_store.user_storage_seckey(db, user)
        try:
            doc = await asyncio.wait_for(nostr_store.get_doc(exodus_vault._port(db), tag, seckey=key, kind=30078, strict=True), timeout=3)
            if doc is not None and (not isinstance(doc, dict) or not isinstance(doc.get('points'), list)):
                return {'available': False, 'points': []}
            points = list((doc or {}).get('points', []))
            if any(not isinstance(p, dict) or number(p.get('at')) is None or number(p.get('usd')) is None for p in points):
                return {'available': False, 'points': []}
            now = int(time.time())
            if valuation['complete'] and (not points or now - points[-1]['at'] >= 900):
                updated = (points + [{'at': now, 'usd': valuation['total']}])[-512:]
                if await asyncio.wait_for(nostr_store.put_doc(exodus_vault._port(db), key, tag, {'points': updated}, kind=30078), timeout=3):
                    points = updated
                else:
                    return {'available': False, 'points': points}
            return {'available': True, 'points': points}
        except Exception:
            # A failed strict read cannot authorize a replacement with an empty chart.
            return {'available': False, 'points': []}
