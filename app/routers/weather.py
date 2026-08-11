"""Weather endpoints for the desktop widget. Thin wrapper over weather_service, which owns the caching
and the reason this is proxied at all (the client's IP and coordinates never reach the upstream).

Public read, like /api/markets: it is a public forecast and carries no user data. The caller supplies the
coordinate, so both handlers validate their inputs — the service's cache is keyed on them."""
from fastapi import APIRouter, HTTPException, Query

from app.services import weather_service

router = APIRouter(prefix="/api/weather", tags=["weather"])


@router.get("")
async def current(lat: float = Query(...), lon: float = Query(...),
                  units: str = Query("metric", pattern="^(metric|imperial)$")):
    """Current conditions + a 3-day outlook for one place, in °C/km-h or °F/mph."""
    # A coordinate off the globe can only be a typo or someone probing the cache key space.
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise HTTPException(status_code=400, detail="lat/lon out of range")
    return await weather_service.forecast(lat, lon, units)


@router.get("/geocode")
async def geocode(q: str = Query(..., min_length=1, max_length=80)):
    """City search, for the widget's 'where are you' picker."""
    return await weather_service.geocode(q)
