"""Auto-split from messages.py: _msg_command."""
from ._common import asyncio, logger, telegram_service
from .keyboards import _help_main_keyboard
from .senders import asyncio, logger, telegram_service


async def _msg_command(_make_tg_node_notify, arg, attachments, chat_id, command, command_service, db, has_images, reply_to, text, user_obj):
                from .webhook import _make_tg_node_notify
                logger.info(f"Executing command: {command} with arg: {arg}, attachments: {len(attachments)}")
                try:
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
                        from app.services import conversation_table
                        tg_conv = await conversation_table.afind_by_title(db, user_obj.id, "📱 Telegram")
                        if tg_conv:
                            # The transcript is relay events now — deleting SQL rows would leave the
                            # history intact and "clear" would do nothing.
                            from app.services import chat_store
                            await chat_store.delete_conversation(db, user_obj, tg_conv.id)
                        await telegram_service.send_message(chat_id, "Conversation cleared. Starting fresh!")
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
