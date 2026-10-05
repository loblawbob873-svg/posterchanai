"""Auto-split from callbacks.py: content callback handlers. Bodies moved verbatim."""
from ._common import ChatService, CommandService, User, _MEDIA_ACTION_TTL, _media_action_cache, _youtube_action_cache, logger, re, telegram_service, time
from .keyboards import _build_torrent_keyboard, _has_nostr, _strip_cmd_links, _strip_hashtags, _torrents_menu_keyboard, re
from .senders import User, _media_action_cache, _offer_social_post, _offer_ytdl_share, _offer_ytdl_video_actions, _send_active_torrents, _send_torrent_results, _strip_cmd_links, logger, re, telegram_service, time


async def _cb_t(update, db, chat_id, data, callback_query, callback_query_id):
        cb_user = db.query(User).filter(
            User.telegram_chat_id == chat_id,
            User.telegram_enabled == True
        ).first()

        if not cb_user:
            await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
            return {"ok": True}

        cb_command_service = CommandService(db, user=cb_user)
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "menu":
            # Show torrents main menu
            await telegram_service.send_message(
                chat_id,
                "🧲 *Torrents Menu*\n\nChoose an option:",
                reply_markup=_torrents_menu_keyboard()
            )
            return {"ok": True}

        elif action == "cat" and len(parts) >= 3:
            # Category browse: send individual result messages
            category = parts[2]
            try:
                await cb_command_service.execute_command("torrents", category)
                await _send_torrent_results(chat_id, category, cb_user.id)
            except Exception as cb_err:
                logger.error(f"Torrent callback error: {cb_err}", exc_info=True)
                await telegram_service.send_message(chat_id, f"Error: {cb_err}")
            return {"ok": True}
        elif action == "dl" and len(parts) >= 4:
            # Download from browse list: t:dl:movies:3
            category = parts[2]
            num = parts[3]
            torrents_arg = f"download {category} {num}"
        elif action in ("pause", "resume", "rm") and len(parts) >= 3:
            # Manage active torrent: t:pause:2
            num = parts[2]
            torrents_arg = f"{action} {num}"
        elif action == "list":
            torrents_arg = "list"
        elif action == "search_hint":
            await telegram_service.send_message(
                chat_id,
                "🔍 Type your torrent search:",
                reply_markup={"force_reply": True, "selective": True, "input_field_placeholder": "e.g. dark knight 1080p"}
            )
            return {"ok": True}
        else:
            return {"ok": True}

        try:
            cb_result = await cb_command_service.execute_command("torrents", torrents_arg)
            cb_content = cb_result.get("content", "")

            cb_arg_parts = torrents_arg.strip().split()
            cb_arg_sub = cb_arg_parts[0].lower() if cb_arg_parts else ""
            if cb_arg_sub in ("list", "ls", "pause", "resume", "rm"):
                # Send each torrent as its own message with buttons beneath it
                await _send_active_torrents(chat_id, cb_content)
            else:
                cb_reply_markup = _build_torrent_keyboard(cb_arg_sub, cb_content, cb_user.id)
                if cb_arg_sub in ("download", "dl", "get") and cb_reply_markup is None:
                    cb_reply_markup = {"inline_keyboard": [[
                        {"text": "📋 Active Downloads", "callback_data": "t:list:0"}
                    ]]}
                cb_content = _strip_cmd_links(cb_content)
                await telegram_service.send_message(chat_id, cb_content, reply_markup=cb_reply_markup)
        except Exception as cb_err:
            logger.error(f"Torrent callback error: {cb_err}", exc_info=True)
            await telegram_service.send_message(chat_id, f"Error: {cb_err}")


async def _cb_ytdlvsend(update, db, chat_id, data, callback_query, callback_query_id):
        import os as _os, tempfile, shutil
        _entry = _media_action_cache.get(chat_id)
        if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
            _media_action_cache.pop(chat_id, None)
            await telegram_service.send_message(chat_id, "⏳ That download expired — run `ytdl video <url>` again.")
            return {"ok": True}
        _vid = next((a for a in _entry["attachments"] if (a[2] or "").startswith("video/")), None)
        if not _vid:
            await telegram_service.send_message(chat_id, "Nothing to send.")
            return {"ok": True}
        _fn, _vbytes, _ = _vid
        if len(_vbytes) > 50 * 1024 * 1024:
            await telegram_service.send_message(
                chat_id,
                f"❌ Too large to send as-is ({len(_vbytes) // (1024*1024)} MB). "
                "Tap 🗜 Compress or ✂️ Clip to shrink it under Telegram's 50 MB limit.")
            return {"ok": True}
        _ymeta = _entry.get("ytdl", {})
        _tmpdir = tempfile.mkdtemp(prefix="tg_ytdlv_send_")
        try:
            _sp = _os.path.join(_tmpdir, _fn)
            with open(_sp, "wb") as _of:
                _of.write(_vbytes)
            _r = await telegram_service.send_video(
                chat_id=chat_id, file_path=_sp,
                caption=_ymeta.get("caption"), duration=_ymeta.get("duration"),
            )
            if not _r.get("ok"):
                await telegram_service.send_message(chat_id, f"❌ Failed to send video: {_r.get('description', _r.get('error', 'Unknown error'))}")
            else:
                await _offer_ytdl_share(chat_id, _fn, _vbytes, db)
        finally:
            shutil.rmtree(_tmpdir, ignore_errors=True)
        return {"ok": True}


async def _cb_n(update, db, chat_id, data, callback_query, callback_query_id):
        cb_user = db.query(User).filter(
            User.telegram_chat_id == chat_id,
            User.telegram_enabled == True
        ).first()

        if not cb_user:
            await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
            return {"ok": True}

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "search_hint":
            await telegram_service.send_message(
                chat_id,
                "🔎 Type your anime search:",
                reply_markup={"force_reply": True, "selective": True, "input_field_placeholder": "e.g. one piece 1080p"}
            )
            return {"ok": True}

        if action == "dl" and len(parts) >= 3:
            nyaa_arg = f"download {parts[2]}"
        else:
            return {"ok": True}

        try:
            cb_command_service = CommandService(db, user=cb_user)
            cb_result = await cb_command_service.execute_command("nyaa", nyaa_arg)
            cb_content = _strip_cmd_links(cb_result.get("content", ""))
            cb_reply_markup = {"inline_keyboard": [[
                {"text": "🔎 New Nyaa Search", "callback_data": "n:search_hint:0"},
                {"text": "📋 Active Downloads", "callback_data": "t:list:0"},
            ]]}
            await telegram_service.send_message(chat_id, cb_content, reply_markup=cb_reply_markup)
        except Exception as cb_err:
            logger.error(f"Nyaa callback error: {cb_err}", exc_info=True)
            await telegram_service.send_message(chat_id, f"Error: {cb_err}")


async def _cb_yt(update, db, chat_id, data, callback_query, callback_query_id):
        action = data.split(":", 1)[1]
        yt_url = _youtube_action_cache.pop(chat_id, None)

        if yt_url is None:
            await telegram_service.send_message(chat_id, "No pending YouTube URL found.")
            return {"ok": True}

        yt_user = db.query(User).filter(
            User.telegram_chat_id == chat_id,
            User.telegram_enabled == True
        ).first()

        if action == "summary":
            await telegram_service.send_message(chat_id, "⏳ Summarizing video, please wait...")
            try:
                yt_cmd_service = CommandService(db, user=yt_user)
                yt_result = await yt_cmd_service.execute_command("yt", yt_url)
                await telegram_service.send_message(chat_id, yt_result.get("content", "Error generating summary."))
            except Exception as yt_err:
                logger.error(f"YouTube summary callback error: {yt_err}", exc_info=True)
                await telegram_service.send_message(chat_id, f"❌ Error: {yt_err}")

        elif action == "post":
            # Generate a social media post for the YouTube video
            await telegram_service.send_message(chat_id, "⏳ Generating social media post...")
            try:
                from app.services.youtube_service import fetch_video_info
                import asyncio as _asyncio
                        
                # Fetch video info for context
                video_info = await _asyncio.wait_for(
                    fetch_video_info(yt_url),
                    timeout=15
                )
                        
                if video_info and video_info.get("title"):
                    video_context = f"Title: {video_info.get('title')}\n\n"
                    if video_info.get("description"):
                        desc = video_info.get("description", "")[:1000]
                        video_context += f"Description: {desc}\n\n"
                    if video_info.get("channel"):
                        video_context += f"Channel: {video_info.get('channel')}\n"
                else:
                    video_context = f"YouTube Video: {yt_url}"
                        
                # Generate social media post
                yt_chat = ChatService(db, user=yt_user)
                yt_chat.num_predict = min(yt_chat.num_predict, 900)
                post_messages = [
                    {
                        "role": "system",
                        "content": "You are a social media expert. Write compelling, detailed social media posts. Output ONLY the post text. No introductions, no 'here is your post', no URL placeholders like 'link' or 'read more'."
                    },
                    {
                        "role": "user",
                        "content": (
                            "Write a viral and engaging social media post for this YouTube video. "
                            "Be detailed — include key facts, context, and why it matters. "
                            "Use emojis.\n\n"
                            f"Content:\n{video_context}"
                        )
                    }
                ]
                post_text = await yt_chat.chat(post_messages)
                post_text = _strip_hashtags(post_text).rstrip() + f"\n\n{yt_url}"
                await _offer_social_post(chat_id, post_text, yt_user, telegram_service)
            except Exception as yt_err:
                logger.error(f"YouTube post generation error: {yt_err}", exc_info=True)
                await telegram_service.send_message(chat_id, f"❌ Error generating post: {yt_err}")

        elif action in ("mp3", "video"):
            from app.services.youtube_service import (
                check_ytdlp_available,
                download_as_mp3,
            )
            import tempfile, shutil, os as _os, asyncio as _asyncio

            if not check_ytdlp_available():
                await telegram_service.send_message(chat_id, "❌ yt-dlp is not installed on the server.")
                return {"ok": True}

            if action == "mp3":
                await telegram_service.send_message(chat_id, "⏳ Downloading MP3, please wait...")
                from app.services import settings_store
                _cookies_v = settings_store.get("ytdl_cookies_path")
                _cookies_path = str(_cookies_v).strip() if _cookies_v else None
                if _cookies_path and not _os.path.isfile(_cookies_path):
                    _cookies_path = None
                _ssl_v = settings_store.get("ytdl_no_ssl_verify")
                _no_ssl = (
                    str(_ssl_v).strip().lower() in ("true", "1", "yes")
                    if _ssl_v else False
                )
                temp_dir = tempfile.mkdtemp(prefix="tg_ytdl_")
                try:
                    dl_result = await _asyncio.to_thread(
                        download_as_mp3, yt_url, temp_dir, _cookies_path, _no_ssl
                    )
                    if not dl_result.success:
                        await telegram_service.send_message(chat_id, f"❌ Download failed: {dl_result.error}")
                        return {"ok": True}
                    file_size = _os.path.getsize(dl_result.local_path)
                    if file_size > 50 * 1024 * 1024:
                        await telegram_service.send_message(
                            chat_id,
                            f"❌ File too large to send via Telegram ({file_size // (1024*1024)} MB). Limit is 50 MB."
                        )
                        return {"ok": True}
                    duration_int = int(dl_result.duration) if dl_result.duration else None
                    await telegram_service.send_audio(
                        chat_id=chat_id,
                        file_path=dl_result.local_path,
                        title=dl_result.title,
                        performer=dl_result.artist,
                        duration=duration_int,
                    )
                except Exception as yt_err:
                    logger.error(f"YouTube MP3 callback error: {yt_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Error: {yt_err}")
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)

            else:  # video
                await telegram_service.send_message(chat_id, "⏳ Downloading video, please wait...")
                import tempfile as _tempfile
                import shutil as _shutil
                import os as _os
                from app.services.youtube_service import download_as_video

                temp_dir = _tempfile.mkdtemp(prefix="tg_ytdlvideo_")
                try:
                    from app.services import settings_store
                    _cookies_v = settings_store.get("ytdl_cookies_path")
                    _cookies_path = str(_cookies_v).strip() if _cookies_v else None
                    if _cookies_path and not _os.path.isfile(_cookies_path):
                        _cookies_path = None
                    _ssl_v = settings_store.get("ytdl_no_ssl_verify")
                    _no_ssl = (
                        str(_ssl_v).strip().lower() in ("true", "1", "yes")
                        if _ssl_v else False
                    )

                    dl_result = await _asyncio.to_thread(
                        download_as_video, yt_url, temp_dir, "best", _cookies_path, _no_ssl
                    )
                    if not dl_result.success:
                        await telegram_service.send_message(chat_id, f"❌ Download failed: {dl_result.error}")
                        return {"ok": True}

                    # Offer Send / Compress / Clip / Clip+Compress (same as the
                    # `ytdl video` command), so a long video can be trimmed/shrunk
                    # instead of bouncing off Telegram's 50 MB send limit.
                    await _offer_ytdl_video_actions(chat_id, dl_result, yt_url, yt_user, db)
                except Exception as yt_err:
                    logger.error(f"YouTube video callback error: {yt_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Error: {yt_err}")
                finally:
                    _shutil.rmtree(temp_dir, ignore_errors=True)
