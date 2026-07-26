"""Auto-split from messages.py: _msg_command."""
from ._common import Conversation, Message, User, _news_post_cache, asyncio, logger, re, telegram_service
from .keyboards import _4chan_initial_keyboard, _build_torrent_keyboard, _has_misskey, _has_nostr, _has_pleroma, _help_main_keyboard, _news_menu_keyboard, _split_news_into_articles, _strip_cmd_links, _torrent_nav_keyboard, re
from .senders import User, _has_misskey, _has_pleroma, _offer_social_post, _offer_ytdl_video_actions, _send_4chan_catalog, _send_active_torrents, _send_nyaa_results, _send_torrent_results, _strip_cmd_links, _torrent_nav_keyboard, asyncio, logger, re, telegram_service


async def _msg_command(_make_tg_node_notify, arg, attachments, chat_id, command, command_service, db, has_images, reply_to, text, user_obj):
                from .webhook import _make_tg_node_notify
                logger.info(f"Executing command: {command} with arg: {arg}, attachments: {len(attachments)}")
                try:
                    # New glowing TEXT post from scratch: `glow <text>` with NO image →
                    # render the neon card and go straight to the social-post offer
                    # (empty body — the text IS the image). Tightly gated so it never
                    # touches `glow`+image (the effect), bare `glow`, or any other
                    # command — those all fall through to the existing handlers below.
                    if command == "glow" and not has_images and arg.strip():
                        try:
                            from app.services import effects_service as _fx
                            _png = await asyncio.to_thread(_fx.render_glow_text_card, arg)
                            import base64 as _b64
                            await telegram_service.send_photo(
                                chat_id, _b64.b64encode(_png).decode("ascii"), "🌟 Glowing text preview")
                            await _offer_social_post(chat_id, "", user_obj, telegram_service,
                                                     prompt="📣 *Post this glowing image?*", image_bytes=_png)
                        except Exception as _ge:
                            logger.error(f"glow text post failed: {_ge}", exc_info=True)
                            await telegram_service.send_message(chat_id, f"❌ Couldn't make the glowing text post: {_ge}")
                        return {"ok": True}
                    if command == "help":
                        await telegram_service.send_message(
                            chat_id,
                            "🤖 *PosterChanAI* — tap a topic to learn more:",
                            parse_mode="MarkdownV2",
                            reply_markup=_help_main_keyboard(),
                        )
                        return {"ok": True}
                    elif command == "new":
                        # Clear the Telegram conversation history for this user
                        tg_conv = db.query(Conversation).filter(
                            Conversation.user_id == user_obj.id,
                            Conversation.title == "📱 Telegram"
                        ).order_by(Conversation.updated_at.desc()).first()
                        if tg_conv:
                            # The transcript is relay events now — deleting SQL rows would leave the
                            # history intact and "clear" would do nothing.
                            from app.services import chat_store
                            await chat_store.delete_conversation(db, user_obj, tg_conv.id)
                            db.commit()
                        await telegram_service.send_message(chat_id, "Conversation cleared. Starting fresh!")
                        return {"ok": True}
                    elif command == "ytdl":
                        if not arg:
                            await telegram_service.send_message(
                                chat_id,
                                "Usage:\n`ytdl <youtube_url>` - Download as MP3\n`ytdl video <youtube_url>` - Download as video"
                            )
                            return {"ok": True}

                        from app.services.youtube_service import (
                            check_ytdlp_available,
                            download_as_mp3,
                            download_as_video,
                            download_video_and_save_to_storage,
                            extract_download_urls,
                        )
                        import tempfile, shutil, os as _os, asyncio as _asyncio

                        if not check_ytdlp_available():
                            await telegram_service.send_message(chat_id, "❌ yt-dlp is not installed on the server.")
                            return {"ok": True}

                        # Check if user wants video or MP3
                        arg_parts = arg.strip().split(maxsplit=1)
                        first_word = arg_parts[0].lower() if arg_parts else ""
                        if first_word == "video" and len(arg_parts) > 1:
                            as_video = True
                            url_arg = arg_parts[1]
                        elif first_word == "mp3" and len(arg_parts) > 1:
                            as_video = False
                            url_arg = arg_parts[1]
                        else:
                            as_video = False
                            url_arg = arg

                        # Optional post-processing modifiers:
                        #   clip <start> <end>   — trim the downloaded video
                        #   compress             — shrink it (applied after clip)
                        # These only apply to video, so their presence implies `video`
                        # even without the keyword (you can't trim/shrink an MP3).
                        _clip_arg = None
                        _toks = url_arg.split()
                        _low = [t.lower() for t in _toks]
                        _compress = "compress" in _low
                        if "clip" in _low:
                            _ci = _low.index("clip")
                            _rest = [t for t in _toks[_ci + 1:_ci + 3] if t.lower() != "compress"]
                            if len(_rest) == 2:
                                _clip_arg = f"{_rest[0]} {_rest[1]}"
                            else:
                                await telegram_service.send_message(chat_id, "❌ `clip` needs a start and end, e.g. `ytdl video <url> clip 0:10 0:30`.")
                                return {"ok": True}
                        if _clip_arg or _compress:
                            as_video = True

                        urls = extract_download_urls(url_arg)
                        if not urls:
                            await telegram_service.send_message(chat_id, "❌ Could not find a valid YouTube URL in your message.")
                            return {"ok": True}

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

                        if as_video:
                            # Download and send video
                            await telegram_service.send_message(chat_id, "⏳ Downloading video, please wait...")
                            temp_dir = tempfile.mkdtemp(prefix="tg_ytdlvideo_")
                            try:
                                dl_result = await _asyncio.to_thread(
                                    download_as_video, urls[0], temp_dir, "best", _cookies_path, _no_ssl
                                )
                                if not dl_result.success:
                                    await telegram_service.send_message(chat_id, f"❌ Download failed: {dl_result.error}")
                                    return {"ok": True}

                                # No inline clip/compress given → offer the actions
                                # interactively (Send / Compress / Clip / Clip+Compress).
                                if not (_clip_arg or _compress):
                                    await _offer_ytdl_video_actions(chat_id, dl_result, urls[0], user_obj, db)
                                    return {"ok": True}

                                # Optional post-processing: clip then compress, reusing the
                                # standalone-command transforms so results are identical.
                                send_path = dl_result.local_path
                                if _clip_arg or _compress:
                                    from app.services.media_service import (
                                        parse_timecode, clip_attachment, compress_attachments,
                                    )
                                    await telegram_service.send_message(chat_id, "⏳ Processing video…")
                                    _fn = _os.path.basename(dl_result.local_path)
                                    with open(dl_result.local_path, "rb") as _vf:
                                        _vbytes = _vf.read()
                                    _mime = "video/mp4"
                                    if _clip_arg:
                                        _p = _clip_arg.split()
                                        _s, _e = parse_timecode(_p[0]), parse_timecode(_p[1])
                                        if _s is None or _e is None or _e <= _s:
                                            await telegram_service.send_message(chat_id, "❌ Invalid clip times — use `clip 0:10 0:30` (end after start).")
                                            return {"ok": True}
                                        _outs, _ = await _asyncio.to_thread(clip_attachment, [(_fn, _vbytes, _mime)], _s, _e)
                                        if not _outs:
                                            await telegram_service.send_message(chat_id, "❌ Clip failed.")
                                            return {"ok": True}
                                        _fn, _vbytes, _mime = _outs[0]["filename"], _outs[0]["data"], _outs[0]["content_type"]
                                    if _compress:
                                        _outs, _ = await _asyncio.to_thread(compress_attachments, [(_fn, _vbytes, _mime)])
                                        if not _outs:
                                            await telegram_service.send_message(chat_id, "❌ Compress failed.")
                                            return {"ok": True}
                                        _fn, _vbytes, _mime = _outs[0]["filename"], _outs[0]["data"], _outs[0]["content_type"]
                                    send_path = _os.path.join(temp_dir, _fn)
                                    with open(send_path, "wb") as _of:
                                        _of.write(_vbytes)

                                file_size = _os.path.getsize(send_path)
                                # Telegram bot limit is 50 MB for videos
                                if file_size > 50 * 1024 * 1024:
                                    # File too large - save to storage and notify. (For a
                                    # clipped/compressed result this re-downloads the full
                                    # source to storage; the trimmed copy can't be stored.)
                                    save_result = await download_video_and_save_to_storage(
                                        url=urls[0],
                                        user_id=user_obj.id,
                                        db=db,
                                        subfolder="YouTube Videos",
                                    )
                                    from app.services.youtube_service import format_download_result
                                    await telegram_service.send_message(
                                        chat_id,
                                        f"❌ Video is too large to send via Telegram ({file_size // (1024*1024)} MB).\n\n{format_download_result(save_result)}"
                                    )
                                    return {"ok": True}

                                # Send the video. After a clip the source duration no
                                # longer matches, so let Telegram infer it from the file.
                                duration_int = None if _clip_arg else (int(dl_result.duration) if dl_result.duration else None)
                                caption = f"🎬 **{dl_result.title}**" if dl_result.title else "🎬 Video"
                                if dl_result.artist:
                                    caption += f"\n👤 {dl_result.artist}"

                                video_result = await telegram_service.send_video(
                                    chat_id=chat_id,
                                    file_path=send_path,
                                    caption=caption,
                                    duration=duration_int,
                                )
                                if not video_result.get("ok"):
                                    error_desc = video_result.get('description', video_result.get('error', 'Unknown error'))
                                    logger.error(f"Failed to send video: {video_result}")
                                    await telegram_service.send_message(chat_id, f"❌ Failed to send video: {error_desc}")
                            except Exception as yt_err:
                                logger.error(f"YouTube video callback error: {yt_err}", exc_info=True)
                                await telegram_service.send_message(chat_id, f"❌ Error: {yt_err}")
                            finally:
                                shutil.rmtree(temp_dir, ignore_errors=True)
                        return {"ok": True}
                    elif command == "torrents":
                        arg_parts = arg.strip().split()
                        arg_sub = arg_parts[0].lower() if arg_parts else ""

                        if not arg_sub:
                            # Show category navigation menu without scraping all categories
                            result = {"type": "text", "content": "🧲 **Torrents** — choose a category:"}
                            reply_markup = _torrent_nav_keyboard()
                        elif arg_sub in ("movies", "tv", "anime", "music", "search", "s"):
                            # Execute to populate the cache, then send individual result messages
                            result = await command_service.execute_command(command, arg)
                            user_id = user_obj.id if user_obj else 0
                            cache_key = "search" if arg_sub in ("search", "s") else arg_sub
                            await _send_torrent_results(chat_id, cache_key, user_id)
                            return {"ok": True}
                        else:
                            if attachments:
                                result = await command_service.execute_command(command, arg, attachments=attachments)
                            else:
                                result = await command_service.execute_command(command, arg)
                            content = result.get("content", "")
                            user_id = user_obj.id if user_obj else 0
                            if arg_sub in ("list", "ls"):
                                await _send_active_torrents(chat_id, content)
                                return {"ok": True}
                            reply_markup = _build_torrent_keyboard(arg_sub, content, user_id)
                            # Clean non-functional links from torrent result text
                            result["content"] = _strip_cmd_links(content)
                    elif command == "nyaa":
                        result = await command_service.execute_command(command, arg)
                        user_id = user_obj.id if user_obj else 0
                        await _send_nyaa_results(chat_id, user_id)
                        return {"ok": True}
                    elif command == "4chan":
                        # Parse board from argument
                        arg_parts = arg.strip().split()
                        board = arg_parts[0].lower() if arg_parts else None
                        allowed_boards = ("g", "pol", "a", "h")
                        
                        if board and board in allowed_boards:
                            # Valid board specified, show catalog
                            user_id = user_obj.id if user_obj else 0
                            await _send_4chan_catalog(chat_id, board, user_id)
                        else:
                            # No board specified or invalid board, show board selector
                            await telegram_service.send_message(
                                chat_id,
                                "🍀 *4chan Board Selector*\n\nChoose a board to browse:",
                                reply_markup=_4chan_initial_keyboard()
                            )
                        return {"ok": True}
                    elif command == "news":
                        # If no argument provided, show the news menu
                        if not arg.strip():
                            await telegram_service.send_message(
                                chat_id,
                                "📰 *News Menu*\n\nChoose an option:",
                                reply_markup=_news_menu_keyboard()
                            )
                            return {"ok": True}
                        
                        # Otherwise, fetch news from specific source
                        result = await command_service.execute_command(command, arg)
                        content = _strip_cmd_links(result.get("content", ""))

                        has_social = _has_misskey(user_obj) or _has_pleroma(user_obj) or _has_nostr(user_obj)

                        articles = _split_news_into_articles(content)
                        if articles:
                            # Cache (title, url) pairs for the Post callbacks
                            _news_post_cache[chat_id] = [(title, url) for (_, title, url, _) in articles]

                            # Send header (date/source summary line) if present
                            header_match = re.match(r'^(##[^\n]+)', content)
                            if header_match:
                                await telegram_service.send_message(chat_id, header_match.group(1))

                            # Send each article as its own message
                            for i, (_, title, url, msg_text) in enumerate(articles[:10], 1):
                                # Build keyboard with Summarize and Post buttons (only if a social platform is configured)
                                buttons = []
                                if has_social:
                                    buttons.append([
                                        {"text": "📝 Summarize", "callback_data": f"news:summarize:{i}"},
                                        {"text": "📣 Post", "callback_data": f"nk:post:{i}"}
                                    ])
                                else:
                                    buttons.append([
                                        {"text": "📝 Summarize", "callback_data": f"news:summarize:{i}"}
                                    ])
                                kbd = {"inline_keyboard": buttons}
                                await telegram_service.send_message(chat_id, msg_text, reply_markup=kbd)
                            return {"ok": True}

                        # Fallback: no articles parsed — send raw content
                        result["content"] = content
                    elif command == "share":
                        # Share command: take the user's text (+ optional attachment) and offer to post it
                        # to configured social platforms directly (no AI generation needed)
                        share_text = arg.strip() if arg.strip() else text.strip()
                        if not share_text and not has_images and not attachments:
                            await telegram_service.send_message(
                                chat_id,
                                "Usage: send `share <your post text>` (optionally attach a photo).\n"
                                "The text will be shared directly to your configured social platforms."
                            )
                            return {"ok": True}

                        _share_user = db.query(User).filter(
                            User.telegram_chat_id == chat_id,
                            User.telegram_enabled == True
                        ).first()

                        # Build share text
                        final_share_text = share_text or "(image)"

                        # Collect image bytes for the social share paths
                        _share_img = None
                        if has_images and attachments:
                            for _fn, _fd, _ct in attachments:
                                if _ct.startswith("image/"):
                                    _share_img = _fd
                                    break
                        # Also check replied-to message for a photo
                        if not _share_img and reply_to:
                            _rt_photos = reply_to.get("photo", [])
                            if _rt_photos:
                                _rt_file_id = _rt_photos[-1].get("file_id")
                                if _rt_file_id:
                                    _rt_fr = await telegram_service.get_file(_rt_file_id)
                                    if _rt_fr and _rt_fr.get("ok"):
                                        _rt_fp = _rt_fr.get("result", {}).get("file_path")
                                        if _rt_fp:
                                            _rt_data = await telegram_service.download_file(_rt_fp)
                                            if _rt_data:
                                                _share_img = _rt_data

                        await _offer_social_post(chat_id, final_share_text, _share_user, telegram_service,
                                                  prompt="📣 *Share this?*", image_bytes=_share_img)
                        return {"ok": True}
                    elif command in ("remind", "reminders"):
                        # Reminders: create/list. For the list, attach a Cancel button per reminder
                        # so Telegram is interactive too (mirrors the web UI's clickable list).
                        result = await command_service.execute_command(command, arg)
                        if result.get("type") == "reminders" and result.get("reminders"):
                            # One message per reminder (plain text — the shared `content` is
                            # Markdown and we send parse_mode="" so it'd show literal ** / _),
                            # each with a single 🗑️ Delete button beneath it.
                            _rems = result["reminders"]
                            await telegram_service.send_message(
                                chat_id, f"⏰ Your reminders ({len(_rems)}):", parse_mode="")
                            for _r in _rems:
                                _line = f"• {_r.get('text','')} — {_r.get('human','')} (id {_r['id']})"
                                await telegram_service.send_message(
                                    chat_id, _line, parse_mode="",
                                    reply_markup={"inline_keyboard": [[
                                        {"text": "🗑️ Delete", "callback_data": f"rem:cancel:{_r['id']}"}]]},
                                )
                        else:
                            await telegram_service.send_message(chat_id, result.get("content", "Done."), parse_mode="")
                        return {"ok": True}
                    elif command in ("pin", "pins"):
                        # Pinned searches: save/list. The list gets a Run + Delete button per item.
                        result = await command_service.execute_command(command, arg)
                        if result.get("type") == "saved_searches" and result.get("saved_searches"):
                            # One message per pin (like torrent/nyaa results): the full pin line as
                            # the body, with Run + Delete buttons right beneath it — so the whole
                            # query is readable and nothing is duplicated/truncated.
                            _pins = result["saved_searches"]
                            await telegram_service.send_message(
                                chat_id, f"📌 Your pins ({len(_pins)}):", parse_mode="")
                            for _s in _pins:
                                await telegram_service.send_message(
                                    chat_id, f"📌 {_s.get('query', '')}",
                                    reply_markup={"inline_keyboard": [[
                                        {"text": "▶ Run", "callback_data": f"pin:run:{_s['id']}"},
                                        {"text": "🗑️ Delete", "callback_data": f"pin:del:{_s['id']}"},
                                    ]]},
                                    parse_mode="", disable_web_page_preview=True,
                                )
                                await asyncio.sleep(0.1)
                        else:
                            await telegram_service.send_message(chat_id, result.get("content", "Done."), parse_mode="")
                        return {"ok": True}
                    else:
                        # For `node` (long jobs finish after this handler returns) and `logs`
                        # (multi-minute agentic health report), stream step progress back to THIS
                        # Telegram chat as it runs.
                        node_notify = _make_tg_node_notify(telegram_service, chat_id) if command in ("node", "logs") else None
                        # Pass attachments to any command that supports them
                        if attachments:
                            result = await command_service.execute_command(command, arg, attachments=attachments, node_notify=node_notify)
                        else:
                            result = await command_service.execute_command(command, arg, node_notify=node_notify)
                    logger.info(f"Command result: {result}")
                except Exception as e:
                    logger.error(f"Command execution error: {e}", exc_info=True)
                    result = {"type": "text", "content": f"Error: {str(e)}"}
                return result
