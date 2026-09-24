"""Read this node's community facts (blocks, leaderboard, activity) from the app's /api/community/*
endpoints -- what the block bot and the daily stats used to read straight out of Pleroma's database.
Authenticated with the bot API key the manager injects (POSTERCHANAI_API_KEY)."""
import json
from urllib import parse, request

from config import POSTERCHANAI_API_ENDPOINT, POSTERCHANAI_API_KEY


class Unavailable(Exception):
    """The app could not be asked. Never the same as "nothing happened"."""


def get(path: str, **params) -> dict:
    base = (POSTERCHANAI_API_ENDPOINT or "http://127.0.0.1:3051").rstrip("/")
    url = f"{base}/api/community/{path}" + ("?" + parse.urlencode(params) if params else "")
    headers = {"Accept": "application/json"}
    if (POSTERCHANAI_API_KEY or "").strip():
        headers["X-API-Key"] = POSTERCHANAI_API_KEY.strip()
    try:
        with request.urlopen(request.Request(url, headers=headers), timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise Unavailable(f"{path}: {type(e).__name__}: {e}") from e
