"""Matrix integration router — login, logout, room listing."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/matrix", tags=["matrix"])


class MatrixConnectRequest(BaseModel):
    homeserver: str
    username: str
    password: str


@router.post("/connect")
async def connect_matrix(
    data: MatrixConnectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Login to Matrix with username/password and store the access token."""
    from app.services.matrix_service import login as matrix_login

    homeserver = data.homeserver.strip().rstrip("/")
    if not homeserver.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Homeserver URL must start with http:// or https://")

    username = data.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    try:
        result = await matrix_login(homeserver, username, data.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Matrix login error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not connect to homeserver: {e}")

    current_user.matrix_enabled = True
    current_user.matrix_homeserver = homeserver
    current_user.matrix_user_id = result["user_id"]
    current_user.matrix_access_token = result["access_token"]
    db.commit()

    return {"ok": True, "user_id": result["user_id"]}


@router.post("/disconnect")
async def disconnect_matrix(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Logout from Matrix and remove stored credentials."""
    if current_user.matrix_access_token and current_user.matrix_homeserver:
        from app.services.matrix_service import logout as matrix_logout
        try:
            await matrix_logout(current_user.matrix_homeserver, current_user.matrix_access_token)
        except Exception as e:
            logger.warning(f"Matrix remote logout failed (continuing): {e}")

    current_user.matrix_enabled = False
    current_user.matrix_homeserver = None
    current_user.matrix_user_id = None
    current_user.matrix_access_token = None
    db.commit()

    return {"ok": True}


@router.get("/rooms")
async def list_rooms(
    current_user: User = Depends(get_current_user),
):
    """Return the list of Matrix rooms the user has joined."""
    if not current_user.matrix_enabled or not current_user.matrix_access_token or not current_user.matrix_homeserver:
        raise HTTPException(status_code=400, detail="Matrix is not connected")

    from app.services.matrix_service import get_joined_rooms

    try:
        rooms = await get_joined_rooms(current_user.matrix_homeserver, current_user.matrix_access_token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Matrix list rooms error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not fetch rooms: {e}")

    return {"rooms": rooms}


class MatrixTestDmRequest(BaseModel):
    bot_user_id: str


@router.post("/test-dm")
async def send_test_dm(
    data: MatrixTestDmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a DM room with the bot user and send a test message."""
    if not current_user.matrix_enabled or not current_user.matrix_access_token or not current_user.matrix_homeserver:
        raise HTTPException(status_code=400, detail="Matrix is not connected")

    bot_user_id = data.bot_user_id.strip()
    if not bot_user_id.startswith("@") or ":" not in bot_user_id:
        raise HTTPException(status_code=400, detail="Invalid Matrix user ID format (expected @user:server)")

    from app.services.matrix_service import create_or_get_dm_room, send_message as matrix_send

    try:
        room_id = await create_or_get_dm_room(
            current_user.matrix_homeserver,
            current_user.matrix_access_token,
            bot_user_id,
        )
        await matrix_send(
            current_user.matrix_homeserver,
            current_user.matrix_access_token,
            room_id,
            "Hello from Posterchanai! Your Matrix bot integration is working.",
        )
        return {"ok": True, "room_id": room_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Matrix test DM error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=str(e))


class MatrixCommandRequest(BaseModel):
    matrix_user_id: str
    command: str


@router.post("/command")
async def execute_matrix_command(
    data: MatrixCommandRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Execute a posterchanai command on behalf of a Matrix user.

    Called by the posterchan Matrix bot. Looks up the posterchanai account
    linked to the sender's Matrix user ID. Optionally authenticated via a
    Bearer API key for additional security when the key is configured.
    """
    from app.models import APIKey

    sender_matrix_id = data.matrix_user_id.strip()
    if not sender_matrix_id:
        raise HTTPException(status_code=400, detail="matrix_user_id is required")

    # Optional Bearer API key — if provided, the key's owner must match the linked Matrix user
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        api_key_obj = db.query(APIKey).filter(
            APIKey.key == token,
            APIKey.is_active == True,
        ).first()
        if not api_key_obj:
            raise HTTPException(status_code=401, detail="Invalid API key")
        user = db.query(User).filter(
            User.id == api_key_obj.user_id,
            User.matrix_enabled == True,
            User.matrix_user_id == sender_matrix_id,
        ).first()
        if not user:
            raise HTTPException(status_code=403, detail="Matrix user is not linked to this account")
    else:
        # No API key — look up directly by linked Matrix user ID
        user = db.query(User).filter(
            User.matrix_enabled == True,
            User.matrix_user_id == sender_matrix_id,
        ).first()
        if not user:
            raise HTTPException(status_code=403, detail="Matrix user is not linked to any account")

    command_str = data.command.strip()
    if not command_str:
        raise HTTPException(status_code=400, detail="Command is required")

    # Handle `post` / `post <url>` — generate a social media post and return the text
    import re as _re
    _post_match = _re.match(r'^post\s+(https?://\S+)', command_str, _re.IGNORECASE)
    if command_str.lower() == "post" or _re.match(r'^post\b', command_str, _re.IGNORECASE):
        url_arg = _post_match.group(1) if _post_match else ""
        # Plain text after "post" (no URL) — use it directly as context
        raw_arg = command_str[4:].strip() if len(command_str) > 4 else ""
        from app.services.chat_service import ChatService as _CS
        from app.services.search_service import SearchService as _SS
        _cs = _CS(db, user=user)
        article_context = url_arg or raw_arg
        if url_arg:
            try:
                import asyncio as _aio
                fetched = await _aio.wait_for(_SS(db).fetch_urls([url_arg], max_urls=1), timeout=15)
                if fetched and fetched[0].get("content") and not fetched[0].get("error"):
                    article_context = f"Title: {fetched[0].get('title','')}\n\n{fetched[0]['content'][:3000]}"
            except Exception:
                pass
        if not article_context:
            return {"result": "Usage: `post <url or text>` — generate a social media post from a URL or topic."}
        _cs.num_predict = min(_cs.num_predict, 900)
        post_text = await _cs.chat([
            {"role": "system", "content": "You are a social media expert. Write a compelling post. Output ONLY the post text. No introductions or meta-commentary."},
            {"role": "user", "content": f"Write a viral and engaging social media post based on this content. Use emojis. Do not use hashtags.\n\nContent:\n{article_context}"},
        ])
        # Strip Markdown links [text](url) → plain url and remove hashtags
        import re as _re2
        post_text = _re2.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\2', post_text)
        post_text = _re2.sub(r'\s*#\w+', '', post_text).strip()
        if url_arg:
            post_text = post_text.rstrip() + f"\n\n{url_arg}"
        # Save for when user replies `share` alone
        from app.models import UserSetting
        _ps = db.query(UserSetting).filter(
            UserSetting.user_id == user.id,
            UserSetting.key == "matrix_pending_post",
        ).first()
        if _ps:
            _ps.value = post_text
        else:
            db.add(UserSetting(user_id=user.id, key="matrix_pending_post", value=post_text))
        db.commit()
        suffix = "\n\n---\nReply `share` to post this to your configured social platforms."
        return {"result": post_text + suffix}

    # Handle `share` / `share <text>` — post text to all configured social platforms
    if command_str.lower().startswith("share"):
        share_text = command_str[5:].strip()
        # No text provided — try to retrieve the pending post from the last `post` command
        if not share_text:
            from app.models import UserSetting
            pending = db.query(UserSetting).filter(
                UserSetting.user_id == user.id,
                UserSetting.key == "matrix_pending_post",
            ).first()
            if pending and pending.value:
                share_text = pending.value
                db.delete(pending)
                db.commit()
            else:
                return {"result": "Nothing to share. Use `post <url>` first, then reply `share`."}
        results = []
        if user.misskey_enabled and user.misskey_instance_url and user.misskey_api_token:
            try:
                from app.services.misskey_service import post_note as _mk
                await _mk(user.misskey_instance_url, user.misskey_api_token, share_text)
                results.append("✅ Misskey")
            except Exception as e:
                results.append(f"❌ Misskey: {e}")
        if user.pleroma_enabled and user.pleroma_instance_url and user.pleroma_access_token:
            try:
                from app.services.pleroma_service import post_status as _plr
                await _plr(user.pleroma_instance_url, user.pleroma_access_token, share_text)
                results.append("✅ Pleroma")
            except Exception as e:
                results.append(f"❌ Pleroma: {e}")
        # Matrix — look up the user's saved bot room from their Matrix account data
        if user.matrix_enabled and user.matrix_homeserver and user.matrix_access_token:
            try:
                from app.services.matrix_service import send_message as _mtx_send
                from urllib.parse import quote as _q
                import httpx as _httpx
                # Get the saved bot room from account data
                _hs = user.matrix_homeserver.rstrip("/")
                _headers = {"Authorization": f"Bearer {user.matrix_access_token}"}
                async with _httpx.AsyncClient(timeout=10) as _client:
                    _whoami = await _client.get(f"{_hs}/_matrix/client/v3/account/whoami", headers=_headers)
                    _uid = _whoami.json().get("user_id", "") if _whoami.status_code == 200 else ""
                    _share_room = None
                    if _uid:
                        _acct = await _client.get(
                            f"{_hs}/_matrix/client/v3/user/{_q(_uid, safe='')}/account_data/posterchanai.bot_room",
                            headers=_headers,
                        )
                        if _acct.status_code == 200:
                            _share_room = _acct.json().get("room_id")
                if _share_room:
                    await _mtx_send(user.matrix_homeserver, user.matrix_access_token, _share_room, share_text)
                    results.append("✅ Matrix")
                else:
                    results.append("⚠️ Matrix: no room configured — use 'Send Test DM' in User Settings first")
            except Exception as e:
                results.append(f"❌ Matrix: {e}")
        if not results:
            return {"result": "No social platforms configured. Connect Misskey, Pleroma, or Matrix in User Settings."}
        return {"result": "\n".join(results)}

    # Parse command
    from app.services.command_service import CommandService
    cmd_service = CommandService(db, user=user)
    command, arg = cmd_service.parse_command(command_str)

    if not command:
        # Not a recognized command — let the AI respond
        from app.services.chat_service import ChatService
        chat_svc = ChatService(db, user=user)
        try:
            reply = await chat_svc.chat([
                {"role": "system", "content": chat_svc.system_prompt},
                {"role": "user", "content": command_str},
            ])
        except Exception as e:
            logger.error(f"Matrix command chat error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
        return {"result": reply}

    try:
        result = await cmd_service.execute_command(command, arg)
        content = result.get("content", "")

        # Strip non-functional cmd: and magnet: links from command output
        import re as _re2
        content = _re2.sub(r'\[([^\]]+)\]\(cmd:[^\)]+\)', r'\1', content)
        content = _re2.sub(r'\[([^\]]+)\]\(magnet:[^\)]+\)', '', content)
        content = _re2.sub(r'\n{3,}', '\n\n', content).strip()

        # Add text-based follow-up hints for interactive commands
        hint = ""
        if command == "torrents":
            arg_lower = arg.strip().lower().split()[0] if arg.strip() else ""
            if arg_lower in ("list", "ls"):
                hint = "\n\n---\n`torrents pause <n>` · `torrents resume <n>` · `torrents rm <n>`"
            elif arg_lower in ("movies", "tv", "anime", "music"):
                hint = f"\n\n---\n`torrents download {arg_lower} <number>` to download"
            elif arg_lower in ("search", "s"):
                hint = "\n\n---\n`torrents download search <number>` to download"
            elif arg_lower in ("pause", "resume", "rm", "download"):
                pass  # action already taken, no hint needed
            elif not arg_lower:
                hint = "\n\n---\nCategories: `torrents movies` · `torrents tv` · `torrents anime` · `torrents music`\nSearch: `torrents search <query>` · Downloads: `torrents list`"
        elif command == "nyaa":
            arg_lower = arg.strip().lower().split()[0] if arg.strip() else ""
            if arg_lower != "download":
                hint = "\n\n---\n`nyaa download <number>` to download"
        elif command == "news":
            hint = "\n\n---\nTo post an article: `post <article url>`"
        elif command == "4chan":
            hint = "\n\n---\nTo view a thread: `4chan <board> <thread_id>`"
        return {"result": content + hint}
    except Exception as e:
        logger.error(f"Matrix command execution error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
