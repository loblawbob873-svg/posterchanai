"""Matrix Client-Server API helpers."""

import logging
import time
import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


async def login(homeserver: str, username: str, password: str) -> dict:
    """Log in to a Matrix homeserver with username/password.

    Returns {"access_token": str, "user_id": str} or raises an exception.
    """
    hs = homeserver.rstrip("/")
    url = f"{hs}/_matrix/client/v3/login"
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code == 403:
            raise ValueError("Invalid username or password")
        if resp.status_code != 200:
            raise ValueError(f"Login failed: HTTP {resp.status_code} — {resp.text[:200]}")
        data = resp.json()
        return {"access_token": data["access_token"], "user_id": data["user_id"]}


async def logout(homeserver: str, access_token: str) -> None:
    """Log out from a Matrix homeserver (invalidates access_token)."""
    hs = homeserver.rstrip("/")
    url = f"{hs}/_matrix/client/v3/logout"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            await client.post(url, headers=headers)
        except Exception as e:
            logger.warning(f"Matrix logout error (ignored): {e}")


async def get_joined_rooms(homeserver: str, access_token: str) -> list[dict]:
    """Return a list of joined rooms as [{"room_id": str, "name": str}, ...].

    Room names are fetched from the m.room.name state event; if unavailable the
    room ID is used as the display name.
    """
    hs = homeserver.rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{hs}/_matrix/client/v3/joined_rooms", headers=headers)
        if resp.status_code != 200:
            raise ValueError(f"Failed to list rooms: HTTP {resp.status_code}")
        room_ids: list[str] = resp.json().get("joined_rooms", [])

        rooms = []
        for room_id in room_ids[:30]:  # cap at 30 rooms
            name = await _room_display_name(client, hs, room_id, headers)
            rooms.append({"room_id": room_id, "name": name})
    return rooms


async def _room_display_name(client: httpx.AsyncClient, hs: str, room_id: str, headers: dict) -> str:
    """Try to get the human-readable room name; fall back to room_id."""
    try:
        from urllib.parse import quote
        encoded = quote(room_id, safe="")
        url = f"{hs}/_matrix/client/v3/rooms/{encoded}/state/m.room.name"
        r = await client.get(url, headers=headers)
        if r.status_code == 200:
            name = r.json().get("name", "").strip()
            if name:
                return name
    except Exception:
        pass
    return room_id


async def send_message(homeserver: str, access_token: str, room_id: str, text: str) -> None:
    """Send a plain-text message to a Matrix room."""
    hs = homeserver.rstrip("/")
    from urllib.parse import quote
    encoded_room = quote(room_id, safe="")
    txn_id = str(int(time.time() * 1000))
    url = f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {"msgtype": "m.text", "body": text}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.put(url, json=payload, headers=headers)
        if resp.status_code not in (200, 201):
            raise ValueError(f"Failed to send Matrix message: HTTP {resp.status_code} — {resp.text[:200]}")
