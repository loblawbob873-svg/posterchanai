"""Matrix integration router — login, logout, room listing."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
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


class MatrixMediaItem(BaseModel):
    filename: str
    data: str  # base64-encoded file content
    content_type: Optional[str] = None


class MatrixCommandRequest(BaseModel):
    matrix_user_id: str
    command: str
    room_id: Optional[str] = None
    # Attached files (base64) forwarded by the bot — used by compress/convert.
    media: Optional[list[MatrixMediaItem]] = None
    # Body of the message the user replied to (Matrix rich-reply). Lets `post`
    # operate on an existing message, mirroring the Telegram reply-to flow.
    reply_text: Optional[str] = None


class MatrixYtdlRequest(BaseModel):
    url: str
    video: bool = False
    clip: Optional[str] = None      # "start end" (e.g. "0:10 0:30"); video only
    compress: Optional[bool] = False  # compress the (clipped) video; video only


class MatrixTimelineActionRequest(BaseModel):
    matrix_user_id: str          # the acting member's mxid → their linked fedi account
    room_id: str
    action: str                  # "like" | "boost" | "reply" | "post"
    target_event_id: Optional[str] = None  # the timeline post being acted on (not for "post")
    thread_root_event_id: Optional[str] = None  # fallback target when target_event_id is an untracked thread child
    text: Optional[str] = None   # body for "reply" / "post"
    emoji: Optional[str] = None  # reaction emoji for "like" (Misskey keeps it; default ❤️)
    media: Optional[list[MatrixMediaItem]] = None  # attachments (base64) for "reply"/"post"


async def _process_matrix_media(cmd_service, command: str, arg: str, data) -> dict:
    """Run an identity-free media transform on the bot-forwarded attachments.

    compress/clip/convert/translate/meme/dildo are pure byte ops and the bot
    uploads the result as itself, so no linked user is required — they're a public
    feature. Returns the {result, files} payload the bot uploads into the room.
    """
    import base64 as _b64
    attachments = []
    for item in (data.media or []):
        try:
            attachments.append((item.filename, _b64.b64decode(item.data), item.content_type or ""))
        except Exception as e:
            logger.warning(f"Matrix {command}: bad media item {item.filename}: {e}")

    # translate: extract text from the uploaded image(s)/PDF(s) and translate it.
    if command == "translate":
        from app.services.document_service import extract_pdf_text, extract_image_text
        from app.services.media_service import is_pdf as _is_pdf, is_image as _is_image
        parts = []
        for _fn, _fdata, _ct in attachments:
            _b = _b64.b64encode(_fdata).decode()
            if _is_pdf(_fn, _ct):
                parts.append(extract_pdf_text(_b) or "")
            elif _is_image(_fn, _ct):
                parts.append(extract_image_text(_b) or "")
        src = "\n\n".join(p for p in parts if p).strip()
        if not src:
            return {"result": "Couldn't extract any text to translate."}
        lang = (arg or "English").strip().title()
        translated = await cmd_service.chat_service.chat([
            {"role": "system", "content": f"Translate the following text to {lang}. Output ONLY the translation, no commentary."},
            {"role": "user", "content": src[:12000]},
        ])
        return {"result": f"🌐 {lang}:\n\n{translated}"}

    result = await cmd_service.execute_command(command, arg, attachments=attachments or None)
    if result.get("type") != "files":
        return {"result": result.get("content", "")}

    # Hand the base64 files back to the bot, which uploads them into the room
    # itself (posting as the bot — consistent with image/ytdl delivery).
    out_files = result.get("files", [])
    return {
        "result": result.get("content", ""),
        "files": [
            {
                "filename": f.get("filename", "file"),
                "data": _b64.b64encode(f["data"]).decode("ascii"),
                "content_type": f.get("content_type", "application/octet-stream"),
            }
            for f in out_files
        ],
    }


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

    # Require a valid Bearer API key. This authenticates the caller as the trusted
    # bot service (not the internet at large), preventing anyone from running
    # commands as an arbitrary linked Matrix user by simply claiming their ID.
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="API key required")
    token = auth_header[7:].strip()
    api_key_obj = db.query(APIKey).filter(
        APIKey.key == token,
        APIKey.is_active == True,
    ).first()
    if not api_key_obj:
        raise HTTPException(status_code=401, detail="Invalid API key")
    # The key authenticates the bot; the command runs as whichever linked Matrix
    # user sent it (any posterchanai user), not the key owner.
    user = db.query(User).filter(
        User.matrix_enabled == True,
        User.matrix_user_id == sender_matrix_id,
    ).first()

    command_str = data.command.strip()
    if not command_str:
        raise HTTPException(status_code=400, detail="Command is required")

    # Identity-free media transforms (compress/clip/convert/translate/meme/dildo) are
    # pure byte ops and the bot uploads the result as itself — a PUBLIC feature any
    # Matrix user can use, even one not linked to a posterchanai account. Handle them
    # before the linked-account requirement below (the bot-API-key auth above is the
    # only gate they need).
    if data.media and command_str.split()[0].lower() in (
        "compress", "clip", "convert", "translate", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "gay", "blacked", "kosher", "barked", "hava", "indian", "yakety"
    ):
        from app.services.command_service import CommandService
        _media_svc = CommandService(db, user=user)
        _mcmd, _marg = _media_svc.parse_command(command_str)
        return await _process_matrix_media(_media_svc, _mcmd, _marg, data)

    if not user:
        raise HTTPException(status_code=403, detail="Matrix user is not linked to any account")

    # Handle `post` / `post <url>` / `post raw <text>` — generate or share a social post.
    import re as _re
    _SHARE_SUFFIX = "\n\n---\nReply `share` to post this to your configured social platforms."

    def _save_pending_post(text: str) -> None:
        from app.models import UserSetting
        _ps = db.query(UserSetting).filter(
            UserSetting.user_id == user.id, UserSetting.key == "matrix_pending_post"
        ).first()
        if _ps:
            _ps.value = text
        else:
            db.add(UserSetting(user_id=user.id, key="matrix_pending_post", value=text))
        db.commit()

    # Body of a replied-to message (Matrix rich-reply), if the bot forwarded one.
    reply_text = (data.reply_text or "").strip()

    if command_str.lower() == "post" or _re.match(r'^post\s', command_str, _re.IGNORECASE):
        raw_arg = command_str[4:].strip() if len(command_str) > 4 else ""

        # `post raw <text>` (also verbatim/as-is/exact) — share text exactly as written, no rewrite.
        # When replying to a message, the reply body is the text to share; the typed
        # words are just the keyword. Otherwise the inline text after the keyword is used.
        _verb_m = _re.match(r'^(raw|verbatim|as-is|asis|exact|exactly)\b\s*(.*)$', raw_arg, _re.IGNORECASE | _re.DOTALL)
        if _verb_m:
            verbatim_text = reply_text or _verb_m.group(2).strip()
            if not verbatim_text:
                return {"result": "Usage: `post raw <text>` (or reply to a message with `post raw`) — share text exactly as written."}
            _save_pending_post(verbatim_text)
            return {"result": verbatim_text + _SHARE_SUFFIX}

        if reply_text:
            # Reply-based: the replied message is the content; typed words are instructions.
            instructions = raw_arg
            _u = _re.findall(r'https?://\S+', reply_text)
            url_arg = _u[0].rstrip('.,)') if _u else ""
        else:
            _post_match = _re.match(r'^post\s+(https?://\S+)', command_str, _re.IGNORECASE)
            url_arg = _post_match.group(1) if _post_match else ""
            # When a URL is given, any remaining words are free-form instructions
            # (e.g. "professional", "don't include links"). For a bare topic the text IS the content.
            instructions = raw_arg.replace(url_arg, "", 1).strip() if url_arg else ""
        _suppress_link = any(p in instructions.lower() for p in (
            "no link", "no links", "without link", "don't include link",
            "dont include link", "do not include link", "exclude link",
            "no url", "without url", "skip link", "no source",
        ))
        from app.services.chat_service import ChatService as _CS
        from app.services.search_service import SearchService as _SS
        _cs = _CS(db, user=user)
        article_context = reply_text or url_arg or raw_arg
        if url_arg:
            try:
                import asyncio as _aio
                fetched = await _aio.wait_for(_SS(db).fetch_urls([url_arg], max_urls=1), timeout=25)
                if fetched and fetched[0].get("content") and not fetched[0].get("error"):
                    article_context = f"Title: {fetched[0].get('title','')}\n\n{fetched[0]['content'][:3000]}"
                else:
                    # Couldn't read the link (e.g. a YouTube video with no captions). Refuse rather
                    # than letting the model invent a post from the bare URL (fetch_urls already
                    # uses the transcript for YouTube, so this only triggers when there's truly none).
                    _err = (fetched[0].get("error") if fetched else "") or "could not fetch content"
                    return {"result": f"Couldn't read that link to write a post. ({_err})"}
            except Exception:
                return {"result": "Couldn't read that link to write a post (fetch error)."}
        if not article_context:
            return {"result": "Usage: `post <url or text>` — generate a social media post from a URL or topic. Use `post raw <text>` to share text exactly as written."}
        _cs.num_predict = min(_cs.num_predict, 900)
        _tone = "compelling" if instructions else "viral and engaging"
        _user_prompt = f"Write a {_tone}, detailed social media post based on this content. Use emojis. Do not use hashtags."
        if instructions:
            _user_prompt += f"\n\nFollow these user instructions exactly: {instructions}"
        _user_prompt += f"\n\nContent:\n{article_context}"
        post_text = await _cs.chat([
            {"role": "system", "content": "You are a social media expert. Write a compelling post. Output ONLY the post text. No introductions or meta-commentary."},
            {"role": "user", "content": _user_prompt},
        ])
        # Strip Markdown links [text](url) → plain url and remove hashtags
        import re as _re2
        post_text = _re2.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\2', post_text)
        post_text = _re2.sub(r'\s*#\w+', '', post_text).strip()
        if url_arg and not _suppress_link:
            post_text = post_text.rstrip() + f"\n\n{url_arg}"
        # Save for when user replies `share` alone
        _save_pending_post(post_text)
        return {"result": post_text + _SHARE_SUFFIX}

    # Handle `share` / `share <text>` — post text to all configured social platforms.
    # Require exact "share" or a "share " prefix so normal chat ("shared the news…")
    # isn't hijacked into the social-posting handler.
    _cs_lower = command_str.lower()
    if _cs_lower == "share" or _cs_lower.startswith("share "):
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
        # Handle `share matrix <number>` — post pending text to a previously listed room
        _share_lower = command_str.lower()
        if _share_lower == "share matrix" or _share_lower.startswith("share matrix "):
            room_arg = command_str[12:].strip()
            from app.models import UserSetting
            _pending = db.query(UserSetting).filter(
                UserSetting.user_id == user.id, UserSetting.key == "matrix_pending_post"
            ).first()
            _rooms_s = db.query(UserSetting).filter(
                UserSetting.user_id == user.id, UserSetting.key == "matrix_pending_rooms"
            ).first()
            # Use saved pending post — never fall back to raw command arg
            pending_text = _pending.value if (_pending and _pending.value) else None
            if not pending_text:
                return {"result": "Nothing pending to share. Use `post <url>` first, then `share` to see room picker."}
            if not room_arg:
                # No room specified — list rooms
                if user.matrix_enabled and user.matrix_access_token and user.matrix_homeserver:
                    from app.services.matrix_service import get_joined_rooms as _rooms
                    rooms = await _rooms(user.matrix_homeserver, user.matrix_access_token)
                    if rooms:
                        import json as _json
                        if _rooms_s:
                            _rooms_s.value = _json.dumps(rooms)
                        else:
                            db.add(UserSetting(user_id=user.id, key="matrix_pending_rooms",
                                               value=_json.dumps(rooms)))
                        db.commit()
                        lines = ["📬 Which Matrix room? Reply `share matrix <number>`:\n"]
                        for i, r in enumerate(rooms[:20], 1):
                            lines.append(f"  {i}. {r['name']}")
                        return {"result": "\n".join(lines)}
                    return {"result": "No Matrix rooms found."}
                return {"result": "Matrix is not connected in User Settings."}
            # Room number provided
            try:
                idx = int(room_arg) - 1
            except ValueError:
                return {"result": "Usage: `share matrix <number>` where number is from the room list."}
            import json as _json
            rooms = _json.loads(_rooms_s.value) if _rooms_s and _rooms_s.value else []
            # No pending room list at all → signal "Nothing pending" so the caller can
            # fall back to other interpretations (e.g. a news-article number).
            if not rooms:
                return {"result": "Nothing pending to share. Use `post <url>` first, then `share` to see room picker."}
            if idx < 0 or idx >= len(rooms):
                return {"result": "Invalid room number. Send `share matrix` to see the list."}
            room_id = rooms[idx]["room_id"]
            room_name = rooms[idx]["name"]
            from app.services.matrix_service import send_message as _mtx_send
            await _mtx_send(user.matrix_homeserver, user.matrix_access_token, room_id, pending_text)
            # Clean up
            if _pending: db.delete(_pending)
            if _rooms_s: db.delete(_rooms_s)
            db.commit()
            return {"result": f"✅ Posted to Matrix room: {room_name}"}

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
        # Matrix — show room picker instead of auto-posting
        if user.matrix_enabled and user.matrix_access_token and user.matrix_homeserver:
            try:
                from app.services.matrix_service import get_joined_rooms as _rooms
                import json as _json
                from app.models import UserSetting
                rooms = await _rooms(user.matrix_homeserver, user.matrix_access_token)
                if rooms:
                    _rooms_s = db.query(UserSetting).filter(
                        UserSetting.user_id == user.id, UserSetting.key == "matrix_pending_rooms"
                    ).first()
                    if _rooms_s:
                        _rooms_s.value = _json.dumps(rooms)
                    else:
                        db.add(UserSetting(user_id=user.id, key="matrix_pending_rooms",
                                           value=_json.dumps(rooms)))
                    # Save share text for when the user picks a room
                    _ps2 = db.query(UserSetting).filter(
                        UserSetting.user_id == user.id, UserSetting.key == "matrix_pending_post"
                    ).first()
                    if _ps2:
                        _ps2.value = share_text
                    else:
                        db.add(UserSetting(user_id=user.id, key="matrix_pending_post", value=share_text))
                    db.commit()
                    lines = ["\n📬 Which Matrix room? Reply `share matrix <number>`:"]
                    for i, r in enumerate(rooms[:20], 1):
                        lines.append(f"  {i}. {r['name']}")
                    results.append("\n".join(lines))
                else:
                    results.append("⚠️ Matrix: no rooms found")
            except Exception as e:
                results.append(f"❌ Matrix: {e}")
        if not results:
            return {"result": "No social platforms configured. Connect Misskey, Pleroma, or Matrix in User Settings."}
        return {"result": "\n".join(results)}

    # Handle `dm @user@host <message>` — send a private (direct-visibility) post to the user(s).
    if _re.match(r'^dm\s', command_str, _re.IGNORECASE):
        dm_rest = command_str[3:].strip()
        _dm_m = _re.match(r'^((?:@\S+\s+)+)(.*)$', dm_rest, _re.DOTALL)
        dm_msg = (_dm_m.group(2).strip() if _dm_m else "")
        if not _dm_m or not dm_msg:
            return {"result": "Usage: `dm @user@host <message>` — send a private direct message."}
        dm_text = " ".join(_dm_m.group(1).split()) + " " + dm_msg
        if user.pleroma_enabled and user.pleroma_instance_url and user.pleroma_access_token:
            try:
                from app.services.pleroma_service import post_status as _plr_dm
                await _plr_dm(user.pleroma_instance_url, user.pleroma_access_token, dm_text, visibility="direct")
                return {"result": f"✅ DM sent ({' '.join(_dm_m.group(1).split())})"}
            except Exception as e:
                return {"result": f"❌ DM failed: {e}"}
        if user.misskey_enabled:
            return {"result": "DMs are currently supported on Pleroma/Mastodon only (Misskey needs the recipient resolved first)."}
        return {"result": "Connect a Pleroma account in User Settings → Social to send DMs."}

    # Matrix-specific help — explains how to use every feature the bot supports.
    if command_str.lower().strip() == "help":
        return {"result": (
            "🤖 *Posterchanai Matrix Bot — Help*\n\n"
            "In a DM you can just type. In a group room, @mention me first.\n\n"
            "📎 *Files (compress / clip / convert / meme)*\n"
            "Upload a file, then send a command — or put the command in the upload's caption:\n"
            "• `compress` — shrink the image(s) or video(s) you uploaded; I post the smaller file back.\n"
            "• `clip <start> <end>` — trim a video to that span, e.g. `clip 0:10 0:30` (also `clip 90 120`).\n"
            "• `convert` — turn image(s) into a single PDF, or a PDF into one image per page.\n"
            "  (Send several images first, then `convert`, to combine them into one PDF.)\n"
            "• `meme <text>` — add outlined white caption text to the lower half of an image.\n"
            "• `dildo` — scatter dildos all over an image.\n"
            "• `poo` — scatter poop all over an image.\n"
            "• `cum` — scatter cum all over an image.\n"
            "• `blood` — splatter blood all over an image.\n"
            "• `bullethole` — punch bullet holes into an image.\n"
            "• `fire` — set an image on fire.\n"
            "• `gay` — stamp a big red GAY on an image.\n"
            "• `blacked` — slap the BLACKED logo on an image.\n"
            "• `kosher` — stamp a 100% KOSHER seal on an image.\n"
            "• `barked` — drop a smirking dog + #BARKED on an image.\n"
            "• `hava` — turn an image into a 6s Hava Nagila video.\n"
            "• `indian` — turn an image into a 6s Indian-song video.\n"
            "• `yakety` — turn an image into a 9s Yakety Sax video.\n"
            "  Uploads are remembered for 5 minutes while you decide.\n\n"
            "🎨 *Create & fetch*\n"
            "• `geni <prompt>` — generate an image.\n"
            "• `screenshot <url>` — full-page screenshot of a website (also `shot` / `ss`).\n"
            "• `yt <url>` or paste a YouTube link — choose summary / audio / video / post.\n"
            "• `ytdl <url>` — download audio; `ytdl video <url>` for video. I upload it to the room.\n"
            "  For video, add `clip <start> <end>` and/or `compress`, e.g. `ytdl video <url> clip 0:10 0:30 compress`.\n\n"
            "🔎 *Search & read*\n"
            "• `search <query>` — web search.\n"
            "• `images <query>` — image search.\n"
            "• `news <source>` — headlines; then `share <n>` to post one.\n"
            "• paste any link — I fetch and summarize the page.\n"
            "• `translate <text> to <lang>`.\n\n"
            "🧲 *Torrents*\n"
            "• `torrents` — browse/search/manage; `nyaa <query>` — anime.\n"
            "• paste a `magnet:?…` link — add it directly.\n\n"
            "📣 *Social*\n"
            "• reply to a message + `post` — turn it into a post; `post raw` shares it as-is; add instructions like `post professional` or `post no links`.\n"
            "• or `post <url or text>` / `post raw <text>` directly.\n"
            "• then `share` — post to your connected platforms.\n"
            "• `dm @user@host <message>` — send a private direct message (Pleroma/Mastodon).\n\n"
            "💰 *Finance*\n"
            "• `budget` — your summary plus a menu of finance actions.\n"
            "• 📋 `bills` (`bills paid` / `bills all`) · ✅ `pay <name>`\n"
            "• ➕ `addbill <name> <amount>` · 💵 `addbill <name> <amount> income`\n"
            "  Connect your account first in the web UI (Settings → Finance).\n\n"
            "🛡️ *Admin* (DM only)\n"
            "• I only auto-accept room invites from admins; invites from anyone else are declined. To add me elsewhere, an admin invites me or uses `join`.\n"
            "• `join <!roomid:server>` or `join <#alias:server>` — I join that room (add `via <server>` to help me find a federated room; for private rooms, invite me).\n"
            "• `leave <!roomid:server>` — I leave that room.\n"
            "• `block <@user:server>` — ignore that user (or a whole server with `:server.org`).\n"
            "• `unblock [<@user:server>]` — un-ignore; with no name, lists who's blocked.\n\n"
            "🛠 *Misc*\n"
            "• `poll <question> | <option 1> | <option 2>` — create a poll (2–20 options, `|`-separated).\n"
            "• `logs` — system logs.\n"
            "• `help` — this message."
        )}

    # Parse command
    from app.services.command_service import CommandService
    cmd_service = CommandService(db, user=user)
    command, arg = cmd_service.parse_command(command_str)

    if not command:
        from app.services.chat_service import ChatService
        chat_svc = ChatService(db, user=user)
        # If the message is a bare URL, fetch the page and summarize it
        import re as _re_url
        _bare = command_str.strip()
        if _re_url.match(r'^https?://\S+$', _bare):
            # Bare URL → fetch and summarize. Do NOT fall through to plain chat on
            # failure: the model would hallucinate a summary of a page it never read.
            try:
                from app.services.search_service import SearchService as _SS
                import asyncio as _aio
                fetched = await _aio.wait_for(_SS(db).fetch_urls([_bare], max_urls=1), timeout=15)
                if fetched and fetched[0].get("content") and not fetched[0].get("error"):
                    ctx = f"Title: {fetched[0].get('title','')}\n\n{fetched[0]['content'][:4000]}"
                    reply = await chat_svc.chat([
                        {"role": "system", "content": "You are a concise summarizer. Output only the summary."},
                        {"role": "user", "content": f"Summarize this page in detail:\n\n{ctx}"},
                    ])
                    return {"result": reply or "Could not summarize the page (empty response)."}
                return {"result": "Could not fetch that page — it may be blocking automated access or be unreachable."}
            except Exception as e:
                logger.warning(f"Matrix link summary fetch failed: {e}")
                return {"result": "Could not fetch that page (timed out or unreachable)."}
        try:
            reply = await chat_svc.chat([
                {"role": "system", "content": chat_svc.system_prompt},
                {"role": "user", "content": command_str},
            ])
        except Exception as e:
            logger.error(f"Matrix command chat error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
        return {"result": reply}

    # Identity-free media transforms (compress/clip/convert/translate/meme/dildo) are
    # handled up front (before the linked-account check) by _process_matrix_media, so
    # they work for any Matrix user — see the top of this endpoint.

    # ytdl: download and send audio/video directly to the Matrix room
    if command == "ytdl" and data.room_id and user.matrix_enabled and user.matrix_access_token:
        from app.services.youtube_service import (
            check_ytdlp_available, download_as_mp3, download_as_video, extract_download_urls
        )
        import tempfile, os as _os, asyncio as _aio
        if not check_ytdlp_available():
            return {"result": "❌ yt-dlp not installed on the server."}
        parts = arg.strip().split(maxsplit=1)
        first = parts[0].lower() if parts else ""
        if first == "video":
            url_arg = parts[1] if len(parts) > 1 else ""
            as_video = True
        elif first == "mp3":
            url_arg = parts[1] if len(parts) > 1 else ""
            as_video = False
        else:
            url_arg = arg
            as_video = False
        urls = extract_download_urls(url_arg)
        if not urls:
            return {"result": "❌ Could not find a valid YouTube URL."}
        from app.models import Setting as _Setting
        _cookies_s = db.query(_Setting).filter(_Setting.key == "ytdl_cookies_path").first()
        _cookies_path = str(_cookies_s.value).strip() if _cookies_s and _cookies_s.value else None
        if _cookies_path and not _os.path.isfile(_cookies_path):
            _cookies_path = None
        _ssl_s = db.query(_Setting).filter(_Setting.key == "ytdl_no_ssl_verify").first()
        _no_ssl = str(_ssl_s.value).strip().lower() in ("true","1","yes") if _ssl_s and _ssl_s.value else False
        tmp = tempfile.mkdtemp(prefix="matrix_ytdl_")
        try:
            if as_video:
                dl = await _aio.to_thread(download_as_video, urls[0], tmp, "best", _cookies_path, _no_ssl)
            else:
                dl = await _aio.to_thread(download_as_mp3, urls[0], tmp, _cookies_path, _no_ssl)
            if not dl.success:
                return {"result": f"❌ Download failed: {dl.error}"}
            with open(dl.local_path, "rb") as f:
                file_bytes = f.read()
            mime = "video/mp4" if as_video else "audio/mpeg"
            fname = _os.path.basename(dl.local_path)
            from urllib.parse import quote as _q
            import httpx as _hx, time as _t
            hs = user.matrix_homeserver.rstrip("/")
            headers = {"Authorization": f"Bearer {user.matrix_access_token}"}
            mxc_uri = None
            async with _hx.AsyncClient(timeout=120.0) as client:
                for media_url in [
                    f"{hs}/_matrix/client/v1/media/upload?filename={_q(fname)}",
                    f"{hs}/_matrix/media/v3/upload?filename={_q(fname)}",
                ]:
                    up = await client.post(media_url, content=file_bytes,
                                           headers={**headers, "Content-Type": mime})
                    if up.status_code in (200, 201):
                        mxc_uri = up.json().get("content_uri")
                        break
                if not mxc_uri:
                    return {"result": "❌ Media upload failed (no content URI returned)."}
                encoded_room = _q(data.room_id, safe="")
                txn_id = str(int(_t.time() * 1000))
                msg_type = "m.video" if as_video else "m.audio"
                payload = {"msgtype": msg_type, "body": fname, "url": mxc_uri,
                           "info": {"mimetype": mime, "size": len(file_bytes)}}
                send_r = await client.put(
                    f"{hs}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}",
                    json=payload, headers=headers)
                if send_r.status_code not in (200, 201):
                    return {"result": f"❌ Failed to send to room: HTTP {send_r.status_code}"}
            # Media already delivered to the room — return empty so the listener
            # doesn't post a redundant text message.
            return {"result": ""}
        except Exception as e:
            logger.error(f"Matrix ytdl error: {e}", exc_info=True)
            return {"result": f"❌ Download error: {e}"}
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    try:
        result = await cmd_service.execute_command(command, arg)

        # generated_image (geni, screenshot): hand the PNG back as a file so the BOT
        # uploads it into the room as itself — works for any user (a DM with the bot)
        # without needing the user's own Matrix account/token, matching compress/convert.
        if result.get("type") == "generated_image" and result.get("image"):
            import base64 as _b64
            img = result["image"]
            if isinstance(img, str) and img.startswith("data:image"):
                img = img.split(",", 1)[1]
            img_b64 = img if isinstance(img, str) else _b64.b64encode(img).decode("ascii")
            return {
                "result": result.get("content", ""),
                "files": [{
                    "filename": "screenshot.png" if command == "screenshot" else "image.png",
                    "data": img_b64,
                    "content_type": "image/png",
                }],
            }

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
        elif command in ("budget", "finance"):
            # Matrix has no inline buttons, so mirror Telegram's finance menu as
            # tap-to-type command shortcuts under the summary.
            hint = ("\n\n---\n"
                    "📋 `bills` · 📜 `bills paid` · 📂 `bills all`\n"
                    "✅ `pay <name>` · ➕ `addbill <name> <amount>` · 💵 `addbill <name> <amount> income`")
        elif command == "bills":
            hint = "\n\n---\n✅ `pay <name>` to pay a bill · `budget` for the summary"
        elif command in ("pay", "addbill"):
            hint = "\n\n---\n`budget` for the updated summary"
        return {"result": content + hint}
    except Exception as e:
        logger.error(f"Matrix command execution error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ytdl")
async def matrix_ytdl_fetch(
    data: MatrixYtdlRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Download a YouTube URL and RETURN the media (base64) for the caller to post.

    Authenticated by the bot's API key only — it does NOT require the requesting
    Matrix user to be linked. This lets the posterchan bot offer `ytdl` to anyone
    in any room (DM or group) and post the media as the bot itself.
    """
    from app.models import APIKey
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="API key required")
    token = auth_header[7:].strip()
    api_key_obj = db.query(APIKey).filter(
        APIKey.key == token, APIKey.is_active == True
    ).first()
    if not api_key_obj:
        raise HTTPException(status_code=401, detail="Invalid API key")

    url = (data.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    from app.services.youtube_service import download_ytdl_bytes
    import os as _os, asyncio as _aio, base64 as _b64

    from app.models import Setting as _Setting
    _cookies_s = db.query(_Setting).filter(_Setting.key == "ytdl_cookies_path").first()
    _cookies_path = str(_cookies_s.value).strip() if _cookies_s and _cookies_s.value else None
    if _cookies_path and not _os.path.isfile(_cookies_path):
        _cookies_path = None
    _ssl_s = db.query(_Setting).filter(_Setting.key == "ytdl_no_ssl_verify").first()
    _no_ssl = str(_ssl_s.value).strip().lower() in ("true", "1", "yes") if _ssl_s and _ssl_s.value else False

    # Cap video at 1080p so files stay within upload limits. 95 MB leaves headroom
    # under Cloudflare's 100 MB request-body cap (the real bottleneck — nginx/Synapse
    # allow much more). Optional clip/compress post-process server-side (clip →
    # compress) and the cap is enforced on the final bytes.
    result = await _aio.to_thread(
        download_ytdl_bytes, url,
        video=bool(data.video), clip=data.clip, compress=bool(data.compress),
        cookies_path=_cookies_path, no_ssl_verify=_no_ssl,
        max_bytes=95 * 1024 * 1024, quality="1080p",
    )
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "filename": result["filename"],
        "mime": result["mime"],
        "data": _b64.b64encode(result["data"]).decode("ascii"),
    }


def _linked_fedi_accounts(user: User) -> list[tuple[str, str, str]]:
    """A user's linked fediverse accounts as (platform, instance_url, token)."""
    accounts: list[tuple[str, str, str]] = []
    if user.misskey_enabled and user.misskey_instance_url and user.misskey_api_token:
        accounts.append(("misskey", user.misskey_instance_url, user.misskey_api_token))
    if user.pleroma_enabled and user.pleroma_instance_url and user.pleroma_access_token:
        accounts.append(("pleroma", user.pleroma_instance_url, user.pleroma_access_token))
    return accounts


def _get_setting(db: Session, key: str, default: str = "") -> str:
    from app.models import Setting
    s = db.query(Setting).filter(Setting.key == key).first()
    return s.value if s and s.value else default


def _unresolved_msg(post) -> str:
    """Explain why a (cross-instance) member's instance couldn't resolve a post to act on."""
    src = (getattr(post, "note_uri", None) or getattr(post, "instance_url", "")) or ""
    host = src.split("://", 1)[-1].split("/", 1)[0] if src else "that server"
    return (f"Your instance couldn't find that post — it may not federate with {host}, "
            "or the post isn't public/visible to you.")


def _full_handle(post) -> str:
    """The post author's full @user@host handle (host filled in from the source instance for
    local authors) so a reply mention resolves cross-instance."""
    acct = (post.author_acct or "").lstrip("@")
    if not acct:
        return ""
    if "@" not in acct:
        host = (post.instance_url or "").split("://", 1)[-1].rstrip("/")
        acct = f"{acct}@{host}" if host else acct
    return f"@{acct}"


def _collapse_ws(s: str) -> str:
    return " ".join((s or "").split())


def _split_shared(text: str) -> tuple[str, str]:
    """Split a shared/quoted message into (quoted_text, comment). Element's "Quote" prefixes the
    quoted lines with '>'; the rest is the member's comment. A plain forward has no '>' lines, so
    the whole message is the quoted text with no comment."""
    lines = text.splitlines()
    quote_lines = [ln.lstrip()[1:].lstrip() for ln in lines if ln.lstrip().startswith(">")]
    if quote_lines:
        comment = "\n".join(ln for ln in lines if not ln.lstrip().startswith(">")).strip()
        return "\n".join(quote_lines).strip(), comment
    return text.strip(), ""


import re as _re_mod
_MATRIX_TO_RE = _re_mod.compile(r'https?://matrix\.to/#/[^\s)>\]]+', _re_mod.IGNORECASE)


def _parse_matrix_to(text: str) -> tuple:
    """If text contains a matrix.to message link (Element's Share → Copy link), return
    (event_id, comment) where comment is the text with the link removed; else (None, text).
    The event id is the last segment of the matrix.to fragment (a `$...` id)."""
    from urllib.parse import unquote
    m = _MATRIX_TO_RE.search(text or "")
    if not m:
        return None, text
    url = m.group(0)
    frag = url.split("#/", 1)[1] if "#/" in url else ""
    frag = frag.split("?", 1)[0]
    last = unquote(frag.split("/")[-1]) if frag else ""
    if not last.startswith("$"):
        return None, text
    comment = (text[:m.start()] + text[m.end():]).strip()
    return last, comment


def _match_delivered_post(db, room_id: str, quoted_text: str):
    """Find a post we delivered to this room whose body matches quoted_text (so sharing it back
    boosts/quotes the ORIGINAL instead of reposting the text as new content)."""
    from app.models import TimelinePost
    target = _collapse_ws(quoted_text)
    if len(target) < 8:            # too short to match confidently
        return None
    rows = (
        db.query(TimelinePost)
        .filter(TimelinePost.room_id == room_id, TimelinePost.body.isnot(None))
        .order_by(TimelinePost.id.desc())
        .limit(300)
        .all()
    )
    for r in rows:
        if _collapse_ws(r.body) == target:
            return r
    return None


def _decode_media(items) -> list:
    """Decode [MatrixMediaItem] → [(bytes, mime)] for posting as fediverse attachments."""
    import base64 as _b64
    out = []
    for it in (items or []):
        try:
            out.append((_b64.b64decode(it.data), it.content_type or ""))
        except Exception as e:
            logger.warning(f"[timeline-action] bad media item {getattr(it, 'filename', '?')}: {e}")
    return out


def _pick_post_account(accounts: list, feed_platform: str) -> tuple:
    """Account for a brand-new post (no source post to match): prefer the platform the room's
    feed comes from, else the first linked account."""
    prefer = [a for a in accounts if a[0] == feed_platform]
    return prefer[0] if prefer else accounts[0]


def _pick_account(accounts: list, post) -> Optional[tuple]:
    """Choose which linked account performs the action: prefer the same instance the post
    was read from, else the same platform, else any linked account (cross-platform resolves
    by canonical AP URI)."""
    src = (post.instance_url or "").rstrip("/")
    same_instance = [a for a in accounts if a[0] == post.platform and a[1].rstrip("/") == src]
    if same_instance:
        return same_instance[0]
    same_platform = [a for a in accounts if a[0] == post.platform]
    if same_platform:
        return same_platform[0]
    return accounts[0] if accounts else None


async def _resolve_target_id(platform: str, instance_url: str, token: str, post) -> Optional[str]:
    """The post's id on the acting account's instance. Same-instance → the stored note_id;
    otherwise resolve by canonical AP URI."""
    from app.services import misskey_service, pleroma_service
    if platform == post.platform and instance_url.rstrip("/") == (post.instance_url or "").rstrip("/"):
        return post.note_id
    if not post.note_uri:
        return None
    if platform == "misskey":
        obj = await misskey_service.resolve_note(instance_url, token, post.note_uri)
        return (obj or {}).get("id")
    status = await pleroma_service.resolve_status(instance_url, token, post.note_uri)
    return (status or {}).get("id")


@router.post("/timeline-action")
async def timeline_action(
    data: MatrixTimelineActionRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Perform a fediverse-timeline interaction (❤ like / 🔁 boost / ↩ reply) on behalf of a
    Matrix member, under that member's own linked Misskey/Pleroma account.

    Called by the posterchan Matrix bot for events in the configured timeline room.
    Authenticated by the bot's Bearer API key (same as /command); the action then runs as
    whichever linked member sent it."""
    from app.models import APIKey, TimelinePost
    from app.services import misskey_service, pleroma_service

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="API key required")
    token = auth_header[7:].strip()
    api_key_obj = db.query(APIKey).filter(APIKey.key == token, APIKey.is_active == True).first()
    if not api_key_obj:
        raise HTTPException(status_code=401, detail="Invalid API key")

    mxid = data.matrix_user_id.strip()
    user = db.query(User).filter(User.matrix_enabled == True, User.matrix_user_id == mxid).first()
    if not user:
        return {"ok": False, "result": "Matrix user is not linked to any account."}

    action = (data.action or "").lower().strip()
    accounts = _linked_fedi_accounts(user)
    if not accounts:
        return {"ok": False, "result": "Connect a Misskey or Pleroma account in User Settings → Social first."}

    # A plain top-level message → a brand-new post under the member's own account. No target
    # post to resolve; the account defaults to the platform the room's feed comes from.
    if action == "post":
        text = (data.text or "").strip()
        media = _decode_media(data.media)
        if not text and not media:
            return {"ok": False, "result": "Nothing to post (empty message)."}
        # Share→boost / share→quote: if the message points at a post we delivered, act on the
        # ORIGINAL (author preserved) instead of reposting the text as fresh content. We detect
        # it two ways: a matrix.to message link (Element Share → Copy link), or the forwarded/
        # quoted text matching a stored post body. Comment present → quote; absent → boost.
        if text and not media:
            from app.models import TimelinePost as _TP
            ev, comment = _parse_matrix_to(text)
            matched = None
            if ev:
                matched = db.query(_TP).filter(_TP.room_id == data.room_id, _TP.event_id == ev).first()
                if not matched:
                    # Link points at something we don't track (a media child, an old event). Do
                    # NOT post the raw matrix.to link to the fediverse — guide the user instead.
                    return {"ok": False, "result": "Couldn't find that post to boost/quote. Reply to the post with `boost` or `quote <comment>` instead."}
            if not matched:
                quoted, comment = _split_shared(text)
                matched = _match_delivered_post(db, data.room_id, quoted)
            if matched:
                mplatform, minstance, mtoken = _pick_account(accounts, matched)
                try:
                    mtarget = await _resolve_target_id(mplatform, minstance, mtoken, matched)
                    if not mtarget:
                        return {"ok": False, "result": _unresolved_msg(matched)}
                    who = f"@{matched.author_acct}" if matched.author_acct else ""
                    if comment:
                        if mplatform == "misskey":
                            await misskey_service.post_note(minstance, mtoken, comment, renote_id=mtarget)
                        else:
                            # Pleroma/Mastodon have no native quote → comment + link to the original.
                            link = matched.note_uri or ""
                            await pleroma_service.post_status(minstance, mtoken, f"{comment}\n\n{link}".strip())
                        return {"ok": True, "result": f"🗣️ quote-posted {who}".strip()}
                    if mplatform == "misskey":
                        await misskey_service.renote(minstance, mtoken, mtarget)
                    else:
                        await pleroma_service.reblog_status(minstance, mtoken, mtarget)
                    return {"ok": True, "result": f"🔁 boosted {who}".strip()}
                except Exception as e:
                    logger.warning(f"[timeline-action] share→boost/quote failed for {mxid}: {e}")
                    return {"ok": False, "result": f"Couldn't boost/quote: {e}"}
        # Safety net: never federate an internal matrix.to link as post content.
        if text:
            text = _MATRIX_TO_RE.sub("", text).strip()
            if not text and not media:
                return {"ok": False, "result": "Nothing to post."}
        feed_platform = _get_setting(db, "fedi_timeline_platform", "misskey")
        platform, instance_url, acct_token = _pick_post_account(accounts, feed_platform)
        try:
            if platform == "misskey":
                note = (await misskey_service.post_note(instance_url, acct_token, text, media=media or None)) or {}
                created = note.get("createdNote") or {}
                new_id, new_uri = created.get("id"), created.get("uri")
                if new_id and not new_uri:
                    new_uri = f"{instance_url.rstrip('/')}/notes/{new_id}"
            else:
                status = (await pleroma_service.post_status(instance_url, acct_token, text, media=media or None)) or {}
                new_id, new_uri = status.get("id"), status.get("uri")
            # Record it so the poller doesn't echo the member's own post back into the room
            # when it shows up in the feed (dedup keys on note_uri/note_id).
            if new_id:
                db.add(TimelinePost(
                    room_id=data.room_id, event_id=f"fedi-post:{new_id}",
                    thread_root_event_id="self",  # not a thread child; just a non-null marker
                    platform=platform, instance_url=instance_url, note_id=new_id,
                    note_uri=new_uri, author_acct=user.matrix_user_id,
                ))
                db.commit()
            return {"ok": True, "result": "✅ posted"}
        except Exception as e:
            logger.warning(f"[timeline-action] post failed for {mxid}: {e}")
            return {"ok": False, "result": f"Post failed: {e}"}

    post = db.query(TimelinePost).filter(
        TimelinePost.room_id == data.room_id,
        TimelinePost.event_id == data.target_event_id,
    ).first()
    if not post and data.thread_root_event_id:
        # target was an untracked thread child (e.g. a media event); fall back to the root.
        post = db.query(TimelinePost).filter(
            TimelinePost.room_id == data.room_id,
            TimelinePost.event_id == data.thread_root_event_id,
        ).first()
    if not post:
        return {"ok": False, "result": "That message isn't a tracked timeline post."}

    platform, instance_url, acct_token = _pick_account(accounts, post)

    try:
        target_id = await _resolve_target_id(platform, instance_url, acct_token, post)
        if not target_id:
            return {"ok": False, "result": _unresolved_msg(post)}

        if action == "like":
            emoji = (data.emoji or "").strip()
            if platform == "misskey":
                # Misskey reactions are per-emoji (incl. custom). Pass the member's emoji
                # through; if the instance rejects it (unknown custom emoji) fall back to ❤️.
                try:
                    await misskey_service.create_reaction(instance_url, acct_token, target_id, reaction=emoji or "❤️")
                except Exception:
                    if not emoji:
                        raise
                    await misskey_service.create_reaction(instance_url, acct_token, target_id, reaction="❤️")
                return {"ok": True, "result": f"{emoji or '❤'} reacted"}
            # Pleroma: try an emoji reaction when a non-heart emoji was used (Pleroma-only
            # endpoint), otherwise/ on failure (e.g. vanilla Mastodon) a plain favourite.
            if emoji and emoji not in ("❤", "❤️", "♥", "♥️"):
                try:
                    await pleroma_service.emoji_react(instance_url, acct_token, target_id, emoji)
                    return {"ok": True, "result": f"{emoji} reacted"}
                except Exception:
                    pass
            await pleroma_service.favourite_status(instance_url, acct_token, target_id)
            return {"ok": True, "result": "❤ favourited"}

        if action == "boost":
            if platform == "misskey":
                await misskey_service.renote(instance_url, acct_token, target_id)
            else:
                await pleroma_service.reblog_status(instance_url, acct_token, target_id)
            return {"ok": True, "result": "🔁 boosted"}

        if action == "reply":
            text = (data.text or "").strip()
            media = _decode_media(data.media)
            if not text and not media:
                return {"ok": False, "result": "No reply text provided."}
            # Auto-mention the author so the reply notifies them (fedi convention) — but NOT if the
            # member's reply already starts with an @mention (they're explicitly addressing someone,
            # e.g. a bot that must be the FIRST mention to trigger), nor if the author's already in.
            mention = _full_handle(post)
            if (mention and text and not text.lstrip().startswith("@")
                    and mention.lower() not in text.lower()):
                text = f"{mention} {text}"
            if platform == "misskey":
                note = (await misskey_service.post_note(instance_url, acct_token, text, reply_id=target_id, media=media or None)) or {}
                created = note.get("createdNote") or {}
                new_id, new_uri = created.get("id"), created.get("uri")
                if new_id and not new_uri:
                    new_uri = f"{instance_url.rstrip('/')}/notes/{new_id}"
            else:
                status = (await pleroma_service.post_status(instance_url, acct_token, text, in_reply_to_id=target_id, media=media or None)) or {}
                new_id, new_uri = status.get("id"), status.get("uri")
            # Record the reply so the descendants poller won't re-post it when it federates
            # back to the source instance (dedup keys on note_uri/note_id). The synthetic
            # event_id keeps the row distinct without a real Matrix event (the member's reply
            # is already visible in the room as their own message).
            if new_id:
                db.add(TimelinePost(
                    room_id=post.room_id, event_id=f"fedi-reply:{new_id}",
                    thread_root_event_id=post.thread_root_event_id or post.event_id,
                    platform=platform, instance_url=instance_url, note_id=new_id,
                    note_uri=new_uri, author_acct=user.matrix_user_id,
                ))
                db.commit()
            return {"ok": True, "result": "↩ reply posted"}

        if action == "quote":
            comment = (data.text or "").strip()
            if not comment:
                return {"ok": False, "result": "Add a comment to quote-post."}
            if platform == "misskey":
                await misskey_service.post_note(instance_url, acct_token, comment, renote_id=target_id)
            else:
                # Pleroma/Mastodon have no native quote → comment + link to the original.
                link = post.note_uri or ""
                await pleroma_service.post_status(instance_url, acct_token, f"{comment}\n\n{link}".strip())
            who = f"@{post.author_acct}" if post.author_acct else ""
            return {"ok": True, "result": f"🗣️ quote-posted {who}".strip()}

        return {"ok": False, "result": f"Unknown action: {action}"}
    except Exception as e:
        logger.warning(f"[timeline-action] {action} failed for {mxid}: {e}")
        return {"ok": False, "result": f"Action failed: {e}"}


class MatrixNotificationReplyRequest(BaseModel):
    matrix_user_id: str
    room_id: str
    target_event_id: str   # the notification DM message being replied to
    thread_root_event_id: Optional[str] = None  # if the reply is in the notification's thread, its
                                                 # root IS the notification event — use as fallback
    text: Optional[str] = None
    media: Optional[list[MatrixMediaItem]] = None   # image/video reply attachments
    probe: Optional[bool] = False   # lookup only: report whether this is a tracked notification,
                                    # do NOT post (lets the bot decide to hold an image for its caption)


@router.post("/notification-reply")
async def notification_reply(
    data: MatrixNotificationReplyRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Post a reply back to the fediverse when a user replies, in their Matrix notification DM,
    to a forwarded notification (mention/reply/favourite/…). Returns {ok:false,"not a
    notification"} when the replied-to message isn't a tracked notification, so the bot can fall
    through to normal handling. Authenticated by the bot's Bearer API key (like /command)."""
    from app.models import APIKey, MatrixNotifyMap
    from app.services import misskey_service, pleroma_service

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="API key required")
    token = auth_header[7:].strip()
    if not db.query(APIKey).filter(APIKey.key == token, APIKey.is_active == True).first():
        raise HTTPException(status_code=401, detail="Invalid API key")

    mxid = data.matrix_user_id.strip()
    user = db.query(User).filter(User.matrix_enabled == True, User.matrix_user_id == mxid).first()
    # Match the replied-to event, OR (for an in-thread reply) the thread root — the notification
    # message is the thread root, while m.in_reply_to points at the last mirrored conversation post.
    event_ids = [e for e in (data.target_event_id, data.thread_root_event_id) if e]
    row = None
    if user:
        row = db.query(MatrixNotifyMap).filter(
            MatrixNotifyMap.room_id == data.room_id,
            MatrixNotifyMap.event_id.in_(event_ids),
            MatrixNotifyMap.user_id == user.id,
        ).order_by(MatrixNotifyMap.id.desc()).first()
    # Probe: just report whether the replied-to event is a tracked notification, without posting.
    # The bot uses this to decide whether to hold an image-only reply for its caption.
    if data.probe:
        return {"ok": bool(row), "is_notification": bool(row), "result": "probe"}
    if not row:
        return {"ok": False, "result": "not a notification"}

    text = (data.text or "").strip()
    media = _decode_media(data.media)
    if not text and not media:
        return {"ok": False, "result": "Empty reply."}
    if row.platform == "misskey":
        if not (user.misskey_instance_url and user.misskey_api_token):
            return {"ok": False, "result": "Your Misskey account isn't connected."}
        inst, tok = user.misskey_instance_url, user.misskey_api_token
    else:
        if not (user.pleroma_instance_url and user.pleroma_access_token):
            return {"ok": False, "result": "Your Pleroma account isn't connected."}
        inst, tok = user.pleroma_instance_url, user.pleroma_access_token
    low = text.lower()
    try:
        # Word shortcuts (no media): act on the notified post instead of replying with the word.
        if not media and low in ("boost", "rt", "repost", "renote", "reblog"):
            if row.platform == "misskey":
                await misskey_service.renote(inst, tok, row.target_id)
            else:
                await pleroma_service.reblog_status(inst, tok, row.target_id)
            return {"ok": True, "result": "🔁 boosted"}
        if not media and low in ("fav", "favourite", "favorite", "like", "+1"):
            if row.platform == "misskey":
                await misskey_service.create_reaction(inst, tok, row.target_id, reaction="❤️")
            else:
                await pleroma_service.favourite_status(inst, tok, row.target_id)
            return {"ok": True, "result": "❤ favourited"}
        # Otherwise post the text and/or image as a reply.
        if row.platform == "misskey":
            await misskey_service.post_note(inst, tok, text, visibility=row.visibility or "public",
                                            reply_id=row.target_id, media=media or None)
        else:
            await pleroma_service.post_status(inst, tok, text, visibility=row.visibility or "public",
                                              in_reply_to_id=row.target_id, media=media or None)
        return {"ok": True, "result": f"↩ replied on {row.platform.title()}"}
    except Exception as e:
        logger.warning(f"[notification-reply] failed for {mxid}: {e}")
        return {"ok": False, "result": f"Reply failed: {e}"}
