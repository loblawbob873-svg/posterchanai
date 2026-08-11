"""Weather for the desktop widget — Open-Meteo, proxied and cached by this node.

WHY THE SERVER FETCHES IT. The client could call Open-Meteo directly; it is free and needs no key. But
then every reader's IP and coordinates go to a third party on a timer, which is the opposite of what
the rest of this app does, and a client behind Tor or on a .onion instance would be sending its real
exit through anyway. One node-side fetch, cached and shared, means the upstream sees this server and a
grid square — never a user.

The cache key is the coordinate ROUNDED to ~1km. Weather does not vary below that, and it turns "every
user's exact location" into a handful of shared buckets, which is both the privacy property and the
reason a hundred widgets cost one request.

Geocoding is a separate, longer-lived cache: a city's coordinates do not change, and the search is
typed a character at a time.
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_TIMEOUT = 15.0

_FORECAST_TTL = 600.0        # 10 minutes; the upstream model does not update faster than hourly
_GEOCODE_TTL = 86400.0
_CACHE_MAX = 400             # bounded — this is a public endpoint and the key comes from the caller

_forecast: dict = {}         # (lat,lon) -> (at, payload)
_geocode: dict = {}          # query -> (at, payload)
_lock: asyncio.Lock = None


def _trim(cache: dict):
    """Oldest-first eviction. Bounded because the key is caller-supplied: without this, walking a
    coordinate grid would grow this dict without limit on a public endpoint."""
    if len(cache) <= _CACHE_MAX:
        return
    for k in sorted(cache, key=lambda k: cache[k][0])[: len(cache) - _CACHE_MAX]:
        cache.pop(k, None)


async def _lock_get() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _get_json(url: str, params: dict):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            logger.warning("[weather] %s HTTP %s", url, r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.warning("[weather] %s failed: %s", url, e)
        return None


async def forecast(lat: float, lon: float) -> dict:
    """Current conditions + a short daily outlook for one place. Never raises."""
    key = (round(float(lat), 2), round(float(lon), 2))
    now = time.time()
    hit = _forecast.get(key)
    if hit and (now - hit[0]) < _FORECAST_TTL:
        return hit[1]
    async with await _lock_get():
        hit = _forecast.get(key)
        if hit and (time.time() - hit[0]) < _FORECAST_TTL:
            return hit[1]
        data = await _get_json(_FORECAST_URL, {
            "latitude": key[0], "longitude": key[1],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "auto", "forecast_days": 3,
        })
        if not data:
            # Serve the stale copy rather than an empty widget — see the module docstring's sibling
            # reasoning in markets_service.get_prices.
            return dict(hit[1], stale=True) if hit else {"ok": False}
        cur = data.get("current") or {}
        daily = data.get("daily") or {}
        out = {
            "ok": True,
            "at": int(time.time()),
            "tz": data.get("timezone") or "",
            "units": {
                "temp": ((data.get("current_units") or {}).get("temperature_2m") or "°C"),
                "wind": ((data.get("current_units") or {}).get("wind_speed_10m") or "km/h"),
            },
            "now": {
                "temp": cur.get("temperature_2m"),
                "feels": cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"),
                "wind": cur.get("wind_speed_10m"),
                "code": cur.get("weather_code"),
                "day": bool(cur.get("is_day")),
            },
            "days": [
                {"date": d, "code": c, "max": mx, "min": mn}
                for d, c, mx, mn in zip(
                    daily.get("time") or [], daily.get("weather_code") or [],
                    daily.get("temperature_2m_max") or [], daily.get("temperature_2m_min") or [],
                )
            ],
        }
        _forecast[key] = (time.time(), out)
        _trim(_forecast)
        return out


async def geocode(q: str) -> dict:
    """City search → a few {name, country, admin, lat, lon}. Never raises."""
    key = " ".join((q or "").strip().lower().split())[:80]
    if not key:
        return {"ok": True, "results": []}
    now = time.time()
    hit = _geocode.get(key)
    if hit and (now - hit[0]) < _GEOCODE_TTL:
        return hit[1]
    data = await _get_json(_GEOCODE_URL, {"name": key, "count": 6, "language": "en", "format": "json"})
    if data is None:
        return hit[1] if hit else {"ok": False, "results": []}
    out = {"ok": True, "results": [
        {
            "name": r.get("name") or "",
            "country": r.get("country") or "",
            "admin": r.get("admin1") or "",
            "lat": r.get("latitude"),
            "lon": r.get("longitude"),
        }
        for r in (data.get("results") or [])
        if isinstance(r.get("latitude"), (int, float)) and isinstance(r.get("longitude"), (int, float))
    ]}
    _geocode[key] = (time.time(), out)
    _trim(_geocode)
    return out
