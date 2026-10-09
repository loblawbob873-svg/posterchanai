"""Auto-split from webhook.py: the message half of _handle_telegram_update."""
from .messages_command import _msg_command
from .messages_chat import _msg_chat
from ._common import ChatService, CommandService, User, _MEDIA_GROUP_CACHE, asyncio, datetime, logger, re, telegram_service, time

from .senders import _send_png_as_document, _send_screenshot, asyncio, logger, re, telegram_service, time

# Telegram matches command words LITERALLY (it never calls parse_command), so it needs its own list —
# but only of the NON-effect commands. The effects come from CommandService, because a second copy of
# them drifts: the hand-written one had already lost `goon`/`hag`, and renaming `anyways` →
# `lookingaway` left the new name falling through to the LLM. Order matters (first match wins), so the
# literals keep theirs and the derived effects — all single words, none of them a prefix of a literal —
# are appended.
_TG_BASE_COMMANDS = [
    "help", "new", "geni", "musicgeni", "videogeni", "narrate", "voice", "talk", "news", "dailynews",
    "logs", "syslogs", "syslog", "healthreport", "node", "screenshot", "shot", "ss",
    "remind", "reminders", "pin", "pins",
    # Sharing a link to Social came back 2026-10-09 ("add ability to share links from chat to Social"):
    # `share <link> [comment]`, `share` as a REPLY to any message with a link, or the link menu's button.
    "share", "post",
]
_TG_EFFECTS = set(CommandService.MOTION_EFFECTS) | set(CommandService.ANIMATED_EFFECTS)
# The effects' OLD names have to be matchable too — aliases are resolved AFTER this match, so a word
# that isn't here never gets as far as COMMAND_ALIASES (that's what keeps `anyways` working).
_TG_EFFECT_WORDS = _TG_EFFECTS | {k for k, v in CommandService.COMMAND_ALIASES.items() if v in _TG_EFFECTS}
# WHAT THE BOT NO LONGER DOES (2026-10-08: "since posterchan is great now, we don't need most of the telegram
# features in the bot" -- kept: AI chat, generation, every notification and alert, the admin tools, reminders and
# pins). These words still MATCH, so they are answered with where the feature lives instead of reaching the chat
# model, which would make an answer up -- the same reason the retired `news` words are still listed.
_TG_MOVED = _TG_EFFECT_WORDS | {
    "ytdl", "yt", "torrents", "nyaa", "search", "images", "mail", "translate",
    "removebackground", "compress", "clip", "convert", "extractaudio", "circlecrop", "ocr", "flashcards",
    "bill", "budget", "bills", "pay", "addbill", "finance",
}
_TG_MOVED_TEXT = ("That's in PosterChan now — open the app or poster.place. Here I can chat, generate images, "
                  "music, video and voices, share links to Social, send you your notifications and alerts, run the "
                  "admin tools, and keep your reminders and pins. Send help for the list.")
_TG_COMMANDS = _TG_BASE_COMMANDS + sorted(_TG_MOVED - set(_TG_BASE_COMMANDS))
# Commands that consume the upload's raw BYTES: OCR'ing the image for them is wasted work (they never
# read the text), and an oversized one has to be reported rather than fed to the chat model.
_TG_RAW_MEDIA_COMMANDS = {
    # `voice` clones the speaker in the attached clip — it needs the audio BYTES, not OCR'd text.
    "voice",
    # `talk` animates the attached PICTURE's mouth — it needs the image bytes, not its OCR.
    "talk",
}


def _shareable_text(msg) -> str:
    """What `share`, sent as a REPLY, puts on Social: the replied-to message's text or caption, plus every
    link Telegram keeps OUTSIDE that text. A channel post's "Read more" or a hyperlinked word is a
    `text_link` entity whose URL appears nowhere in `text`, so sharing the text alone would post the
    headline and lose the link -- the one thing the person meant to share."""
    if not isinstance(msg, dict):
        return ""
    body = (msg.get("text") or msg.get("caption") or "").strip()
    links = []
    for e in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
        u = (e or {}).get("url") if (e or {}).get("type") == "text_link" else None
        if u and u.startswith(("http://", "https://")) and u not in body and u not in links:
            links.append(u)
    return "\n".join(([body] if body else []) + links).strip()


async def _handle_message(update, db):
    from .webhook import _make_tg_node_notify
    try:
        message = update.get("message")
        if message:
            
            chat_id = str(message.get("chat", {}).get("id"))
            # Get text OR caption (Telegram sends caption separately for photos)
            text = message.get("text", "") or message.get("caption", "")
            user = message.get("from", {})
            username = user.get("username", "unknown")
            
            # Check for reply_to_message (when user replies to a message)
            reply_to = message.get("reply_to_message", {})
            reply_text = reply_to.get("text", "") if reply_to else ""

            # Detect replies to bot ForceReply prompts and route them as commands.
            # We identify our prompts by their exact text content.
            _FORCE_REPLY_ROUTES = {
                "🎨 Describe the image you want to generate:": "geni",
                "📸 Send the URL to screenshot:": "screenshot",
            }
            reply_from = (reply_to or {}).get("from", {})
            if reply_from.get("is_bot") and text.strip():
                route = _FORCE_REPLY_ROUTES.get(reply_text.strip())
                if route:
                    text = f"{route} {text.strip()}"
                    text_lower = text.lower()
                    reply_to = {}
                    reply_text = ""

            # Reply to a forwarded social notification → post it back to that platform.
            # Checked before command/intent handling so the freeform reply isn't misread.
            _reply_msg_id = (reply_to or {}).get("message_id")
            if _reply_msg_id and text.strip() and reply_from.get("is_bot"):
                from app.services import social_notifications_service
                try:
                    _social_resp = await social_notifications_service.handle_reply(
                        db, chat_id, _reply_msg_id, text.strip()
                    )
                except Exception as _e:
                    logger.warning(f"[social] reply handling error: {_e}")
                    _social_resp = "❌ Failed to send reply."
                if _social_resp is not None:
                    await telegram_service.send_message(chat_id, _social_resp)
                    return {"ok": True}

            # Detect forwarded messages
            is_forwarded = bool(
                message.get("forward_date") or
                message.get("forward_origin") or
                message.get("forward_from") or
                message.get("forward_from_chat")
            )
            
            # Check for attachments (photos, documents, videos)
            # Photos in Telegram messages are in a list - get the highest res (last one)
            photos = message.get("photo", [])
            document = message.get("document", [])
            # Video / animation (GIF) attachments — used by the compress command
            video = message.get("video") or message.get("animation")
            
            logger.warning(f"TELEGRAM: text={len(text or '')} chars, reply_to={len(reply_text or '')} chars, "
                           f"photos={len(photos) if photos else 0}")
            
            # Strip /no_think prefix — it's a Qwen3 control token, not a user query.
            # If it appears verbatim in the message the model describes it instead of obeying it.
            # chat_service no longer injects /no_think unconditionally; strip_thinking_tags
            # already cleans thinking blocks from every response.
            if text.lower().startswith("/no_think"):
                text = text[len("/no_think"):].strip()
                if not text:
                    # User sent /no_think with no message — just confirm and wait for next message.
                    await telegram_service.send_message(
                        chat_id,
                        "✅ Got it — I'll respond directly without thinking.\n\nJust send your message now."
                    )
                    return {"ok": True}

            # Convert text to lowercase for command matching
            text_lower = text.lower().strip()

            # --- Authorization check ---
            # Allow /start <key> for account linking; block all other messages from unlinked users.
            _auth_user = db.query(User).filter(
                User.telegram_chat_id == chat_id,
                User.telegram_enabled == True
            ).first()

            if not _auth_user:
                if text.startswith("/start "):
                    import hmac
                    from sqlalchemy.exc import IntegrityError
                    key = text.replace("/start ", "").strip()
                    keyed_user = db.query(User).filter(User.telegram_key == key).first()
                    # Constant-time compare as defense-in-depth (DB already did the lookup)
                    key_valid = (
                        keyed_user is not None
                        and hmac.compare_digest(keyed_user.telegram_key or "", key)
                        and (
                            keyed_user.telegram_key_expires_at is None
                            or keyed_user.telegram_key_expires_at > datetime.utcnow()
                        )
                    )
                    if key_valid:
                        # Reject if this user is already linked to a different Telegram chat
                        if keyed_user.telegram_enabled and keyed_user.telegram_chat_id and keyed_user.telegram_chat_id != chat_id:
                            await telegram_service.send_message(
                                chat_id,
                                "This account is already linked to a different Telegram chat. Unlink it first from User Settings."
                            )
                            return {"ok": True}
                        try:
                            keyed_user.telegram_chat_id = chat_id
                            keyed_user.telegram_enabled = True
                            keyed_user.telegram_key = None
                            keyed_user.telegram_key_expires_at = None
                            db.commit()
                            await telegram_service.send_message(
                                chat_id,
                                f"Your Telegram account has been linked to {keyed_user.username}! You can now use the bot."
                            )
                        except IntegrityError:
                            db.rollback()
                            await telegram_service.send_message(
                                chat_id,
                                "This Telegram chat is already linked to a different user. Unlink it first from that account's settings."
                            )
                    else:
                        await telegram_service.send_message(
                            chat_id,
                            "Invalid or expired key. Please generate a new key from User Settings - Telegram and try again."
                        )
                else:
                    await telegram_service.send_message(
                        chat_id,
                        "Your Telegram account is not linked. Generate a key from User Settings - Telegram tab and send /start <key> to this bot."
                    )
                return {"ok": True}

            # Check if the message starts with a known command
            command = None
            arg = text
            commands = _TG_COMMANDS
            for cmd in commands:
                if text_lower.startswith(cmd + " ") or text_lower == cmd:
                    command = cmd
                    arg = text[len(cmd):].strip()
                    break
            # Telegram skips parse_command, so resolve aliases (e.g. shot/ss -> screenshot) to the
            # canonical name, or execute_command rejects them as "Unknown command".
            if command:
                command = CommandService.COMMAND_ALIASES.get(command, command)

            # A one-word MISTYPING of a command must not reach the model. `syslgos` did, and the
            # model answered with a fabricated boot sequence — kernel version, an sshd "Accepted
            # connection from 192.168.1 (external)", a sudo-to-root line, an OOM kill — plus an
            # analysis calling it "classic privilege escalation". Ask instead of guessing: running
            # the guess outright is wrong when the catalogue contains `node`.
            if command is None and not photos:
                _sug = CommandService.did_you_mean(text)
                if _sug:
                    await telegram_service.send_message(
                        chat_id, f"Did you mean: {_sug}\n\nSend {_sug} to run it.")
                    return {"ok": True}

            # User is guaranteed to be linked at this point (auth check above)
            user_obj = _auth_user
            logger.info(f"Found user: {user_obj.username}")

            # Process the message - check for commands first
            chat_service = ChatService(db, user=user_obj)
            command_service = CommandService(db, user=user_obj)
            text_lower = text.lower().strip()
            
            logger.info(f"Telegram message: '{text}'")
            
            # Process attachments (photos, documents) - download first
            attachments = []
            has_images = False
            ocr_text = None
            # Cloud Bot API caps bot downloads at 20 MiB; a local Bot API server
            # raises it to ~2 GB. Track any oversized attachment so compress/convert
            # can explain why it can't be processed.
            TELEGRAM_MAX_DOWNLOAD_BYTES = (2000 * 1024 * 1024) if telegram_service.is_local_api else (20 * 1024 * 1024)
            oversized_attachment = None  # (filename, size_bytes)
            
            # Check if the message starts with a known command
            command = None
            arg = text
            commands = _TG_COMMANDS
            for cmd in commands:
                if text_lower.startswith(cmd + " ") or text_lower == cmd:
                    command = cmd
                    arg = text[len(cmd):].strip()
                    break
            # Telegram skips parse_command, so resolve aliases (e.g. shot/ss -> screenshot) to the
            # canonical name, or execute_command rejects them as "Unknown command".
            if command:
                command = CommandService.COMMAND_ALIASES.get(command, command)
            # `share` as a REPLY shares the message it answers; anything typed after it is the comment.
            if command == "post" and reply_to:
                shared = _shareable_text(reply_to)
                if shared:
                    arg = (arg + "\n\n" + shared).strip() if arg else shared
            # FEATURES THAT LIVE IN POSTERCHAN NOW (2026-10-08: "since posterchan is great now, we don't need most
            # of the telegram features in the bot"). The words still MATCH -- unmatched, `torrents` or `compress`
            # would go to the chat model, which would invent an answer -- and say where the feature is instead.
            if command and command in _TG_MOVED:
                await telegram_service.send_message(chat_id, _TG_MOVED_TEXT)
                return {"ok": True}

            logger.warning(f"TELEGRAM: text={len(text or '')} chars, cmd={command}, arg={len(arg or '')} chars, "
                           f"photos={len(photos) if photos else 0}")
            
            # Download photos FIRST (before any command processing that needs OCR)
            if photos:
                logger.info(f"Processing {len(photos)} photos from Telegram")
                if photos:
                    photo = photos[-1]  # Get highest resolution
                    file_id = photo.get("file_id")
                    logger.info(f"Using photo file_id: {file_id}")
                    if file_id:
                        # Get the file path from Telegram
                        file_result = await telegram_service.get_file(file_id)
                        logger.info(f"File result: {file_result}")
                        if file_result and file_result.get("ok"):
                            file_path = file_result.get("result", {}).get("file_path")
                            logger.info(f"File path: {file_path}")
                            if file_path:
                                # Download the file
                                downloaded_data = await telegram_service.download_file(file_path)
                                if downloaded_data:
                                    import base64
                                    b64_size = len(base64.b64encode(downloaded_data))
                                    attachments.append(("photo.jpg", downloaded_data, "image/jpeg"))
                                    has_images = True
                                    logger.info(f"Downloaded photo, data size: {len(downloaded_data)}, base64 size: {b64_size}")
                                else:
                                    logger.warning("Failed to download photo data")
            
            # Download document
            if document:
                file_id = document.get("file_id")
                file_name = document.get("file_name", "document")
                doc_size = document.get("file_size") or 0
                if file_id and doc_size > TELEGRAM_MAX_DOWNLOAD_BYTES:
                    oversized_attachment = (file_name, doc_size)
                    logger.warning(f"Document {file_name} is {doc_size} bytes — exceeds Telegram bot download limit")
                elif file_id:
                    logger.info(f"Processing document: {file_name}")
                    file_result = await telegram_service.get_file(file_id)
                    if file_result.get("ok"):
                        file_path = file_result.get("result", {}).get("file_path")
                        if file_path:
                            downloaded_data = await telegram_service.download_file(file_path)
                            if downloaded_data:
                                # Determine content type — prefer Telegram's mime_type,
                                # fall back to the filename extension.
                                content_type = document.get("mime_type") or "application/octet-stream"
                                lname = file_name.lower()
                                if lname.endswith('.pdf'):
                                    content_type = "application/pdf"
                                elif lname.endswith(('.jpg', '.jpeg')):
                                    content_type = "image/jpeg"
                                elif lname.endswith('.png'):
                                    content_type = "image/png"
                                elif lname.endswith('.gif'):
                                    content_type = "image/gif"
                                elif lname.endswith(('.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v')):
                                    content_type = "video/mp4"
                                attachments.append((file_name, downloaded_data, content_type))
                                logger.info(f"Downloaded document: {file_name}, size: {len(downloaded_data)}")

            # Download video / animation attachments (for the compress command)
            if video:
                file_id = video.get("file_id")
                v_size = video.get("file_size") or 0
                if file_id and v_size > TELEGRAM_MAX_DOWNLOAD_BYTES:
                    oversized_attachment = (video.get("file_name") or "video.mp4", v_size)
                    logger.warning(f"Video is {v_size} bytes — exceeds Telegram bot download limit")
                    file_id = None  # skip the doomed getFile call
                if file_id:
                    v_name = video.get("file_name") or "video.mp4"
                    v_mime = video.get("mime_type") or "video/mp4"
                    file_result = await telegram_service.get_file(file_id)
                    if file_result.get("ok"):
                        file_path = file_result.get("result", {}).get("file_path")
                        if file_path:
                            downloaded_data = await telegram_service.download_file(file_path)
                            if downloaded_data:
                                attachments.append((v_name, downloaded_data, v_mime))
                                logger.info(f"Downloaded video: {v_name}, size: {len(downloaded_data)}")

            # Handle Telegram media groups: multiple docs sent together arrive as separate webhooks
            # with the same media_group_id. Accumulate them before processing.
            media_group_id = message.get("media_group_id")
            if media_group_id and attachments:
                _mg = _MEDIA_GROUP_CACHE.setdefault(
                    media_group_id, {"attachments": [], "text": "", "created_at": time.time()}
                )
                if text.strip():
                    _mg["text"] = text  # caption rides on whichever message has it
                _mg["attachments"].extend(attachments)
                _mg["last"] = time.time()
                # Album photos arrive as SEPARATE webhooks and download at different
                # speeds, so wait until the group has been QUIET for ~1.5s rather than a
                # fixed sleep — otherwise the fastest handler popped before the others had
                # added their image (symptom: only 1 image was used). Each late arrival
                # bumps `last`, so this keeps waiting until the whole album is in.
                while True:
                    await asyncio.sleep(1.5)
                    _cur = _MEDIA_GROUP_CACHE.get(media_group_id)
                    if _cur is None:
                        return {"ok": True}  # another handler already processed the group
                    if time.time() - _cur.get("last", 0) >= 1.4:
                        break
                _mg_data = _MEDIA_GROUP_CACHE.pop(media_group_id, None)
                if _mg_data is None:
                    return {"ok": True}
                attachments = _mg_data["attachments"]
                text = _mg_data["text"] or text
                text_lower = text.lower().strip()
                # Re-derive the command from the ASSEMBLED caption: the handler that wins
                # the pop may be a caption-less photo, so the `command` parsed earlier could
                # be None even though the album carries a caption like "whoabuddy".
                command = None
                arg = text
                for cmd in commands:
                    if text_lower.startswith(cmd + " ") or text_lower == cmd:
                        command = cmd
                        arg = text[len(cmd):].strip()
                        break
                if command:
                    command = CommandService.COMMAND_ALIASES.get(command, command)
                logger.info(f"[MEDIA-GROUP] {media_group_id}: assembled {len(attachments)} attachments, cmd={command}, text={len(text or '')} chars")

            # Extract text from PDF/Office document attachments (concatenate all, not just last)
            doc_text = None
            pdf_attachments = []  # collect raw PDF bytes for potential merge
            if attachments:
                import base64 as _b64
                from app.services.document_service import extract_pdf_text, extract_document_text, merge_pdfs
                doc_parts = []
                for _fname, _fdata, _ctype in attachments:
                    try:
                        _fdata_b64 = _b64.b64encode(_fdata).decode('utf-8')
                        if _ctype == "application/pdf" or _fname.lower().endswith('.pdf'):
                            pdf_attachments.append((_fname, _fdata))
                            _extracted = extract_pdf_text(_fdata_b64)
                            if _extracted:
                                doc_parts.append(f"[PDF: {_fname}]\n\n{_extracted}")
                                logger.info(f"Extracted {len(_extracted)} chars from PDF: {_fname}")
                        elif _ctype not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                            _extracted = extract_document_text(_fdata_b64)
                            if _extracted:
                                doc_parts.append(f"[Document: {_fname}]\n\n{_extracted}")
                                logger.info(f"Extracted {len(_extracted)} chars from document: {_fname}")
                    except Exception as _doc_err:
                        logger.error(f"Document extraction error for {_fname}: {_doc_err}")
                if doc_parts:
                    doc_text = "\n\n---\n\n".join(doc_parts)

            # OCR every image up front so a later step can use the text — but NOT for the commands
            # that work on the RAW FILE (compress/convert/every effect): they never read it, so the
            # OCR is pure latency on the upload path.
            if has_images and attachments and command not in _TG_RAW_MEDIA_COMMANDS:
                for filename, file_data, content_type in attachments:
                    if content_type.startswith("image/"):
                        import base64
                        image_b64 = base64.b64encode(file_data).decode('utf-8')
                        try:
                            from app.services.document_service import extract_image_text
                            ocr_result = extract_image_text(image_b64)
                            if ocr_result:
                                ocr_text = ocr_result
                                logger.info(f"Extracted OCR text: {len(ocr_text)} chars")
                        except Exception as e:
                            logger.error(f"OCR error: {e}")
                        break
            
            # Attachment too large for Telegram to hand to the bot (20 MB cap).
            # Handle here so it works whether or not a command caption was given,
            # instead of falling through to the chat model.
            if oversized_attachment and (command is None or command in _TG_RAW_MEDIA_COMMANDS):
                _ov_name, _ov_size = oversized_attachment
                _cap_mb = TELEGRAM_MAX_DOWNLOAD_BYTES / (1024 * 1024)
                if telegram_service.is_local_api:
                    _msg = (
                        f"❌ `{_ov_name}` is {_ov_size / (1024 * 1024):.1f} MB, over the "
                        f"{_cap_mb:.0f} MB limit of the configured Bot API server."
                    )
                else:
                    _msg = (
                        f"❌ `{_ov_name}` is {_ov_size / (1024 * 1024):.1f} MB. The cloud Telegram Bot "
                        f"API only lets bots download files up to 20 MiB (≈20.97 MB).\n\n"
                        f"Use the **web UI** for larger files, or enable a local Bot API server "
                        f"in Admin → Nodes."
                    )
                await telegram_service.send_message(chat_id, _msg)
                return {"ok": True}

            reply_markup = None
            if command:
                _r = await _msg_command(_make_tg_node_notify, arg, attachments, chat_id, command, command_service, db, has_images, reply_to, text, user_obj)
                if not (isinstance(_r, dict) and "type" in _r):
                    return _r if isinstance(_r, dict) else {"ok": True}
                result = _r
                reply_markup = result.pop("reply_markup", None)
            else:
                # Regular chat - check for images and do OCR or pass to vision model
                _r = await _msg_chat(attachments, chat_id, chat_service, command_service, db, doc_text, has_images, is_forwarded, message, reply_text, text, user_obj)
                if not (isinstance(_r, dict) and "type" in _r):
                    return _r if isinstance(_r, dict) else {"ok": True}
                result = _r
            
            # Handle the result
            response_type = result.get("type", "text")
            response_content = result.get("content", "")
            image_data = result.get("image")
            
            # Clean response content - remove template artifacts and any leaked thinking
            if response_content:
                from app.services.text_utils import strip_thinking_tags
                response_content = strip_thinking_tags(response_content)
                # Remove template tokens
                for pattern in [r'\[INST\]', r'\[/INST\]', r'INST\]', r'<\|im_end\|>', r'<\|im_start\|>']:
                    response_content = re.sub(pattern, '', response_content, flags=re.IGNORECASE)
                # Remove orphan brackets
                response_content = re.sub(r'\[(?=\s|$)', '', response_content)
                response_content = re.sub(r'^\]', '', response_content)
                response_content = response_content.strip()
                
                if not response_content:
                    response_content = "I didn't get a proper response. Please try again."
            
            logger.info(f"Result type: {response_type}, has image: {bool(image_data)}")
            
            if response_type == "generated_image" and image_data and result.get("prefer_document"):
                # Screenshots: deliver document-first (full resolution) and skip the
                # photo/social-share path, which compresses the image too small to read.
                logger.info(f"Screenshot detected, sending as document, image length: {len(image_data)}")
                await _send_screenshot(chat_id, image_data, response_content)
            elif response_type == "generated_image" and image_data:
                logger.info(f"Generated image detected, sending via Telegram, image length: {len(image_data)}")
                photo_result = await telegram_service.send_photo(chat_id, image_data, response_content)
                if not photo_result.get("ok"):
                    logger.error(f"Failed to send photo: {photo_result}")
                    # Telegram rejects photos that are too tall/large (common for full-page
                    # screenshots) — retry as a document, which has far looser limits.
                    if not await _send_png_as_document(chat_id, image_data, response_content):
                        await telegram_service.send_message(chat_id, f"{response_content}\n\n(Image failed to send)")
            elif response_type == "generated_video" and result.get("video"):
                # Branded MP4 from musicgeni (song-over-bg) OR videogeni (generated clip): decode to
                # a temp file and send as a Telegram video.
                import base64 as _mv_b64, tempfile as _mv_tmp, os as _mv_os
                _mv_path = None
                try:
                    _mv_bytes = _mv_b64.b64decode(result["video"])
                    fd, _mv_path = _mv_tmp.mkstemp(prefix="tg_music_", suffix=".mp4")
                    with _mv_os.fdopen(fd, "wb") as _f:
                        _f.write(_mv_bytes)
                    _mv_res = await telegram_service.send_video(chat_id, _mv_path, caption=response_content)
                    if not _mv_res.get("ok"):
                        logger.error(f"Failed to send generated music video: {_mv_res}")
                        await telegram_service.send_message(chat_id, f"{response_content}\n\n(Song failed to send)")
                except Exception as _mv_err:
                    logger.error(f"Generated music video send error: {_mv_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, "🎵 Couldn't deliver the generated song.")
                finally:
                    if _mv_path and _mv_os.path.exists(_mv_path):
                        try:
                            _mv_os.unlink(_mv_path)
                        except Exception:
                            pass
            elif response_type == "generated_audio" and result.get("audio"):
                # Generated song (musicgeni): decode the base64 audio to a temp file and send it
                # as a Telegram audio message.
                import base64 as _mg_b64, tempfile as _mg_tmp, os as _mg_os
                _mg_fmt = (result.get("format") or "mp3").lower()
                _mg_path = None
                try:
                    _mg_bytes = _mg_b64.b64decode(result["audio"])
                    fd, _mg_path = _mg_tmp.mkstemp(prefix="tg_music_", suffix="." + _mg_fmt)
                    with _mg_os.fdopen(fd, "wb") as _f:
                        _f.write(_mg_bytes)
                    _mg_res = await telegram_service.send_audio(
                        chat_id, _mg_path, title="PosterChanAI", caption=response_content,
                    )
                    if not _mg_res.get("ok"):
                        logger.error(f"Failed to send generated audio: {_mg_res}")
                        await telegram_service.send_message(chat_id, f"{response_content}\n\n(Song failed to send)")
                except Exception as _mg_err:
                    logger.error(f"Generated audio send error: {_mg_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, "🎵 Couldn't deliver the generated song.")
                finally:
                    if _mg_path and _mg_os.path.exists(_mg_path):
                        try:
                            _mg_os.unlink(_mg_path)
                        except Exception:
                            pass
            elif response_type == "search":
                # Send AI summary, then append top result links
                search_results = result.get("results", [])
                links = ""
                if search_results:
                    link_lines = []
                    for r in search_results[:5]:
                        title = (r.get("title") or r.get("url", ""))[:60]
                        url = r.get("url", "")
                        if url:
                            link_lines.append(f"• [{title}]({url})")
                    if link_lines:
                        links = "\n\n**Sources:**\n" + "\n".join(link_lines)
                await telegram_service.send_message(chat_id, response_content + links)
            elif response_type == "images":
                images = result.get("images", [])
                if not images:
                    await telegram_service.send_message(chat_id, response_content)
                else:
                    await telegram_service.send_message(chat_id, response_content)
                    for img in images:
                        img_url = img.get("img_src", "")
                        page_url = img.get("url", img_url)
                        title = (img.get("title") or "")[:80]
                        if not img_url:
                            continue
                        caption = f"[{title}]({page_url})" if title and page_url else title
                        photo_result = await telegram_service.send_photo(chat_id, img_url, caption or None)
                        if not photo_result.get("ok"):
                            logger.warning(f"Could not send image {img_url}: {photo_result.get('description', '')}")
                        await asyncio.sleep(0.15)
            elif response_type == "files":
                # compress/convert output — send each file back as a document
                files = result.get("files", [])
                if response_content:
                    await telegram_service.send_message(chat_id, response_content)
                for f in files:
                    f_bytes = f.get("data")
                    f_name = f.get("filename", "file")
                    if not f_bytes:
                        continue
                    send_result = await telegram_service.send_document_bytes(chat_id, f_bytes, f_name)
                    if not send_result.get("ok"):
                        logger.error(f"Failed to send file {f_name}: {send_result}")
                        await telegram_service.send_message(chat_id, f"❌ Failed to send {f_name}")
                    await asyncio.sleep(0.15)
            else:
                await telegram_service.send_message(chat_id, response_content, reply_markup=reply_markup)

            return {"ok": True}
    except Exception as e:
        logger.error(f"Telegram message handler error: {e}", exc_info=True)
