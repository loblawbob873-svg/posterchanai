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
        if resp.status_code in (401, 403):
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

        import asyncio
        capped = room_ids[:30]
        names = await asyncio.gather(
            *[_room_display_name(client, hs, rid, headers) for rid in capped]
        )
        rooms = [{"room_id": rid, "name": name} for rid, name in zip(capped, names)]
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
    """Send a message to a Matrix room with URL auto-linking."""
    import re as _re
    hs = homeserver.rstrip("/")
    from urllib.parse import quote
    encoded_room = quote(room_id, safe="")
    txn_id = str(int(time.time() * 1000))
    url = f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    # Build formatted_body with clickable links
    import html as _html
    escaped = _html.escape(text)
    formatted = _re.sub(
        r'(https?://\S+)',
        r'<a href="\1">\1</a>',
        escaped,
    )
    payload: dict = {"msgtype": "m.text", "body": text}
    if formatted != escaped:
        payload["format"] = "org.matrix.custom.html"
        payload["formatted_body"] = formatted
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.put(url, json=payload, headers=headers)
        if resp.status_code not in (200, 201):
            raise ValueError(f"Failed to send Matrix message: HTTP {resp.status_code} — {resp.text[:200]}")


def _detect_mime(image_bytes: bytes) -> tuple[str, str]:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "image.jpg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "image.png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "image.gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp", "image.webp"
    return "image/jpeg", "image.jpg"


async def send_image(homeserver: str, access_token: str, room_id: str,
                     image_bytes: bytes, caption: str = "", mime: str = "") -> None:
    """Upload image bytes to Matrix media and send as m.image in a room."""
    hs = homeserver.rstrip("/")
    from urllib.parse import quote
    detected_mime, filename = _detect_mime(image_bytes)
    mime = mime or detected_mime
    headers_auth = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Upload media — try v1 endpoint first, fall back to legacy
        for media_url in [
            f"{hs}/_matrix/client/v1/media/upload?filename={filename}",
            f"{hs}/_matrix/media/v3/upload?filename={filename}",
        ]:
            upload_resp = await client.post(
                media_url,
                content=image_bytes,
                headers={**headers_auth, "Content-Type": mime},
            )
            if upload_resp.status_code in (200, 201):
                mxc_uri = upload_resp.json().get("content_uri")
                break
        else:
            raise ValueError(f"Media upload failed: HTTP {upload_resp.status_code} — {upload_resp.text[:200]}")

        if not mxc_uri:
            raise ValueError("Media upload returned no content_uri")

        encoded_room = quote(room_id, safe="")
        txn_id = str(int(time.time() * 1000))
        send_url = f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}"
        payload = {
            "msgtype": "m.image",
            "body": caption or "image.png",
            "url": mxc_uri,
            "info": {
                "mimetype": mime,
                "size": len(image_bytes),
            },
        }
        # If there's a caption send it as a separate text message after the image
        resp = await client.put(send_url, json=payload, headers=headers_auth)
        if resp.status_code not in (200, 201):
            raise ValueError(f"Failed to send Matrix image: HTTP {resp.status_code} — {resp.text[:200]}")

        # Send caption as a follow-up text message if provided
        if caption and caption != "image.png":
            txn_id2 = str(int(time.time() * 1000) + 1)
            send_url2 = f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id2}"
            await client.put(send_url2, json={"msgtype": "m.text", "body": caption}, headers=headers_auth)


async def _ensure_joined(client: httpx.AsyncClient, hs: str, room_id: str, headers: dict) -> bool:
    """Join a room if not already a member. Returns True if joined/already joined."""
    from urllib.parse import quote
    encoded = quote(room_id, safe="")
    r = await client.post(f"{hs}/_matrix/client/v3/join/{encoded}", json={}, headers=headers)
    return r.status_code in (200, 201)


async def _is_encrypted(client: httpx.AsyncClient, hs: str, room_id: str, headers: dict) -> bool:
    """Return True if the room has the m.room.encryption state event (E2EE enabled)."""
    from urllib.parse import quote
    encoded = quote(room_id, safe="")
    r = await client.get(
        f"{hs}/_matrix/client/v3/rooms/{encoded}/state/m.room.encryption",
        headers=headers,
    )
    return r.status_code == 200


async def create_or_get_dm_room(homeserver: str, access_token: str, bot_user_id: str) -> str:
    """Return an unencrypted DM room ID with bot_user_id, creating one if needed.

    Iterates m.direct candidates, skipping any that are encrypted or unjoinable.
    Always creates a new room if no suitable unencrypted room is found.
    """
    from urllib.parse import quote
    hs = homeserver.rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # Get own user_id
        whoami = await client.get(f"{hs}/_matrix/client/v3/account/whoami", headers=headers)
        user_id = whoami.json().get("user_id", "") if whoami.status_code == 200 else ""

        if user_id:
            dm_r = await client.get(
                f"{hs}/_matrix/client/v3/user/{quote(user_id, safe='')}/account_data/m.direct",
                headers=headers,
            )
            if dm_r.status_code == 200:
                for candidate in dm_r.json().get(bot_user_id, []):
                    # Skip encrypted rooms — bot cannot read or reply in E2EE rooms
                    if await _is_encrypted(client, hs, candidate, headers):
                        logger.info(f"Skipping encrypted DM room {candidate}")
                        continue
                    # Ensure we're a joined member
                    joined = await _ensure_joined(client, hs, candidate, headers)
                    if joined:
                        logger.info(f"Using existing unencrypted DM room {candidate}")
                        return candidate
                    logger.debug(f"Skipping DM room {candidate} — join failed")

        # Create a new unencrypted DM room
        logger.info(f"Creating new unencrypted DM room with {bot_user_id}")
        resp = await client.post(
            f"{hs}/_matrix/client/v3/createRoom",
            json={
                "invite": [bot_user_id],
                "is_direct": True,
                "preset": "trusted_private_chat",
                "initial_state": [],  # No m.room.encryption — keeps the room unencrypted
            },
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            raise ValueError(f"Failed to create DM room: HTTP {resp.status_code} — {resp.text[:200]}")
        return resp.json()["room_id"]
