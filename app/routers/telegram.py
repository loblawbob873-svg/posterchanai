from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import logging
import re
import asyncio
import time
from datetime import datetime, timedelta

# Accumulate Telegram media group messages (multiple docs sent at once)
# dict[media_group_id] -> {"attachments": list, "text": str, "created_at": float}
_MEDIA_GROUP_CACHE: dict = {}

from app.database import get_db, SessionLocal
from app.models import User, Setting, Conversation, Message
from app.auth import get_current_user, get_admin_user
from app.services.telegram_service import telegram_service, configure_from_settings as _configure_telegram
from app.services.chat_service import ChatService
from app.services.command_service import CommandService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Torrent inline keyboard helpers
# ---------------------------------------------------------------------------

def _split_news_into_articles(content: str) -> list:
    """Split news markdown into individual (source_name, title, url, message_text) tuples."""
    results = []

    # Split multiple sources on the --- divider
    source_sections = re.split(r'\n\n---\n\n', content)

    for section in source_sections:
        # Extract source name from **Name:** line
        source_match = re.search(r'\*\*([^*]+)\*\*', section)
        source_name = source_match.group(1).rstrip(':').strip() if source_match else 'News'

        # Each article starts with "- [title](url)" then optional indented summary lines
        article_re = re.compile(
            r'-\s+\[([^\]]+)\]\((https?://[^)]+)\)([\s\S]*?)(?=\n-\s+\[|\Z)',
            re.MULTILINE,
        )
        for m in article_re.finditer(section):
            title   = m.group(1).strip()
            url     = m.group(2).strip()
            summary = m.group(3).strip()

            # Build the per-article Telegram message
            msg = f"📰 *{source_name}*\n\n[{title}]({url})"
            if summary:
                msg += f"\n\n{summary}"

            results.append((source_name, title, url, msg))

    return results


def _make_tg_node_notify(telegram_service, chat_id):
    """Build an async callback that DMs a finished `node` job's output to this chat.
    Used so long-running node jobs started from Telegram report back here when done."""
    async def _notify(job):
        # Agent step-streaming passes a plain string (e.g. "⚙️ `cmd`"); job-completion passes a Job.
        if isinstance(job, str):
            try:
                await telegram_service.send_message(str(chat_id), job)
            except Exception as e:
                logger.warning(f"[node] telegram step notify failed: {e}")
            return
        from app.services.node_service import tail, INLINE_LIMIT
        icon = {"done": "✅", "failed": "❌", "killed": "🛑"}.get(job.status, "ℹ️")
        out = (job.output or "(no output)").strip()
        text = (
            f"{icon} Job #{job.id} on `{job.node}` {job.status} (exit {job.exit_code})\n"
            f"`{job.command}`\n\n```\n{tail(out, 3000)}\n```"
        )
        try:
            await telegram_service.send_message(str(chat_id), text)
            # Long output: also deliver the full thing as a .txt document.
            if len(out) > INLINE_LIMIT:
                await telegram_service.send_document_bytes(
                    str(chat_id), out.encode("utf-8", "replace"), f"node-{job.node}-job{job.id}.txt"
                )
        except Exception as e:
            logger.warning(f"[node] telegram notify failed for job #{job.id}: {e}")
    return _notify


def _strip_cmd_links(text: str) -> str:
    """Remove [text](cmd:...) and [text](magnet:...) links that don't render in Telegram."""
    # Remove [text](cmd:...) — non-clickable in Telegram
    text = re.sub(r'\[([^\]]+)\]\(cmd:[^\)]+\)', '', text)
    # Remove [text](magnet:...) — too long / non-clickable
    text = re.sub(r'\[([^\]]+)\]\(magnet:[^\)]+\)', '', text)
    # Clean up orphan leading/trailing pipes left after link removal
    text = re.sub(r'^\s*\|\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*\|\s*$', '', text, flags=re.MULTILINE)
    # Collapse runs of blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _build_torrent_keyboard(arg_sub: str, content: str, user_id: int) -> Optional[dict]:
    """Return a Telegram inline_keyboard dict for torrent results, or None."""
    from app.services.command_service import _torrent_cache

    if arg_sub in ("movies", "tv", "anime", "music"):
        cached = _torrent_cache.get(user_id, {}).get(arg_sub, [])
        if not cached:
            return None
        buttons: list = []
        row: list = []
        for i in range(1, len(cached) + 1):
            row.append({"text": f"📥 {i}", "callback_data": f"t:dl:{arg_sub}:{i}"})
            if len(row) == 5:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        # Back to category nav
        buttons.append([
            {"text": "🎬 Movies", "callback_data": "t:cat:movies"},
            {"text": "📺 TV", "callback_data": "t:cat:tv"},
            {"text": "🎵 Music", "callback_data": "t:cat:music"},
            {"text": "🎌 Anime", "callback_data": "t:cat:anime"},
        ])
        return {"inline_keyboard": buttons} if buttons else None

    elif arg_sub in ("search", "s"):
        cached = _torrent_cache.get(user_id, {}).get("search", [])
        if not cached:
            return None
        buttons = []
        row = []
        for i in range(1, len(cached) + 1):
            row.append({"text": f"📥 {i}", "callback_data": f"t:dl:search:{i}"})
            if len(row) == 5:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([
            {"text": "🎬 Movies", "callback_data": "t:cat:movies"},
            {"text": "📺 TV", "callback_data": "t:cat:tv"},
            {"text": "🎵 Music", "callback_data": "t:cat:music"},
            {"text": "🎌 Anime", "callback_data": "t:cat:anime"},
        ])
        return {"inline_keyboard": buttons} if buttons else None

    elif arg_sub == "nyaa":
        from app.services.command_service import _nyaa_cache
        cached = _nyaa_cache.get(user_id, [])
        if not cached:
            return None
        buttons = []
        row = []
        for i in range(1, min(len(cached) + 1, 11)):  # max 10 buttons
            row.append({"text": f"📥 {i}", "callback_data": f"n:dl:{i}"})
            if len(row) == 5:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([
            {"text": "🔎 New Nyaa Search", "callback_data": "n:search_hint:0"},
            {"text": "📋 Active Downloads", "callback_data": "t:list:0"},
        ])
        return {"inline_keyboard": buttons} if buttons else None

    elif arg_sub in ("list", "ls"):
        # Count active torrents from the formatted result text
        rm_matches = re.findall(r'cmd:torrents rm (\d+)', content)
        count = len(rm_matches)
        if count == 0:
            return {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": "t:list:0"}]]}
        buttons = []
        for i in range(1, count + 1):
            # Detect pause vs resume state from the content
            if f"torrents resume {i}" in content:
                toggle = {"text": f"#{i} ▶ Resume", "callback_data": f"t:resume:{i}"}
            else:
                toggle = {"text": f"#{i} ⏸ Pause", "callback_data": f"t:pause:{i}"}
            buttons.append([
                toggle,
                {"text": f"#{i} 🗑 Remove", "callback_data": f"t:rm:{i}"},
            ])
        buttons.append([{"text": "🔄 Refresh", "callback_data": "t:list:0"}])
        return {"inline_keyboard": buttons}

    return None


def _torrent_nav_keyboard() -> dict:
    """Return a fresh category navigation keyboard dict."""
    return {
        "inline_keyboard": [
            [
                {"text": "🎬 Movies", "callback_data": "t:cat:movies"},
                {"text": "📺 TV", "callback_data": "t:cat:tv"},
            ],
            [
                {"text": "🎵 Music", "callback_data": "t:cat:music"},
                {"text": "🎌 Anime", "callback_data": "t:cat:anime"},
            ],
            [
                {"text": "🔍 Search…", "callback_data": "t:search_hint:0"},
                {"text": "🔎 Nyaa Search", "callback_data": "n:search_hint:0"},
            ],
            [
                {"text": "📋 Active Downloads", "callback_data": "t:list:0"},
            ],
        ]
    }


def _4chan_initial_keyboard() -> dict:
    """Return initial board selection keyboard when user types '4chan' without a board."""
    return {
        "inline_keyboard": [
            [
                {"text": "🖥 /g/ Technology", "callback_data": "4c:board:g:0"},
                {"text": "🌎 /pol/", "callback_data": "4c:board:pol:0"},
            ],
            [
                {"text": "🇯🇵 /a/ Anime", "callback_data": "4c:board:a:0"},
                {"text": "🔞 /h/ Hentai", "callback_data": "4c:board:h:0"},
            ],
        ]
    }


def _4chan_board_keyboard(board: str = "g") -> dict:
    """Return 4chan board selection keyboard."""
    return {
        "inline_keyboard": [
            [
                {"text": "🖥 /g/ Technology", "callback_data": "4c:board:g:0"},
                {"text": "🌎 /pol/", "callback_data": "4c:board:pol:0"},
            ],
            [
                {"text": "🇯🇵 /a/ Anime", "callback_data": "4c:board:a:0"},
                {"text": "🔞 /h/ Hentai", "callback_data": "4c:board:h:0"},
            ],
        ]
    }


def _4chan_thread_keyboard(board: str, thread_id: int, has_summary: bool = False, offset: int = 0, total_posts: int = 0) -> dict:
    """Build inline keyboard for viewing a 4chan thread."""
    buttons = []
    posts_per_page = 10
    
    # First row: Summarize and Refresh
    row1 = []
    if not has_summary:
        row1.append({"text": "📝 Summarize", "callback_data": f"4c:summarize:{board}:{thread_id}"})
    row1.append({"text": "🔄 Refresh", "callback_data": f"4c:refreshthread:{board}:{thread_id}:{offset}"})
    if row1:
        buttons.append(row1)
    
    # Second row: Pagination buttons (Previous/Next)
    row2 = []
    if offset > 0:
        prev_offset = max(0, offset - posts_per_page)
        row2.append({"text": "⬅️ Previous", "callback_data": f"4c:prevpage:{board}:{thread_id}:{prev_offset}"})
    # Show Next button if there are more replies after current page
    total_replies = total_posts - 1  # Exclude OP
    remaining_replies = total_replies - offset - posts_per_page
    if remaining_replies > 0:
        next_offset = offset + posts_per_page
        row2.append({"text": "Next ➡️", "callback_data": f"4c:nextpage:{board}:{thread_id}:{next_offset}"})
    if row2:
        buttons.append(row2)
    
    # Third row: Open on 4chan
    buttons.append([
        {"text": "🔗 Open on 4chan", "url": f"https://boards.4chan.org/{board}/thread/{thread_id}"},
    ])
    # Fourth row: Back to catalog
    buttons.append([
        {"text": "⬅️ Back to Catalog", "callback_data": f"4c:board:{board}"},
    ])
    return {"inline_keyboard": buttons}


def _torrents_menu_keyboard() -> dict:
    """Return torrents main menu keyboard."""
    return {
        "inline_keyboard": [
            [
                {"text": "🎬 Movies", "callback_data": "t:cat:movies"},
                {"text": "📺 TV", "callback_data": "t:cat:tv"},
            ],
            [
                {"text": "🎵 Music", "callback_data": "t:cat:music"},
                {"text": "🎌 Anime", "callback_data": "t:cat:anime"},
            ],
            [
                {"text": "🔍 Search Torrents", "callback_data": "t:search_hint:0"},
            ],
            [
                {"text": "📋 Active Downloads", "callback_data": "t:list:0"},
            ],
            [
                {"text": "🔎 Nyaa Search", "callback_data": "n:search_hint:0"},
            ],
        ]
    }


def _clean_4chan_text(text: str) -> str:
    """Clean 4chan HTML text for Telegram display.
    
    Decodes HTML entities and escapes Telegram markdown characters.
    """
    import html
    if not text:
        return ""
    # Decode HTML entities (&gt; -> >, &lt; -> <, &quot; -> ", etc.)
    text = html.unescape(text)
    # Escape Telegram markdown chars
    text = text.replace("*", "\\*").replace("_", "\\_").replace("[", "\\[").replace("]", "\\]")
    return text


def _format_4chan_post(post: dict, max_len: int = 800) -> str:
    """Format a single 4chan post for Telegram display.
    
    Handles 4chan's HTML content: converts <br> to newlines, strips other tags,
    decodes HTML entities, and escapes Telegram markdown characters.
    """
    import html
    
    name = post.get("name", "Anonymous")
    com = post.get("com", "")
    no = post.get("no", 0)
    
    if not com:
        com = ""
    
    # Convert <br> tags to newlines first (4chan uses these for line breaks)
    com = re.sub(r"<br\s*/?>", "\n", com, flags=re.IGNORECASE)
    
    # Remove other HTML tags (quotes, links, spans, etc.)
    com = re.sub(r"<[^>]+>", "", com)
    
    # Decode HTML entities (&gt; -> >, &lt; -> <, &quot; -> ", etc.)
    com = html.unescape(com)
    
    # Clean up whitespace (collapse multiple spaces, but preserve newlines)
    lines = com.split("\n")
    lines = [" ".join(line.split()) for line in lines]  # Collapse spaces per line
    com = "\n".join(line for line in lines if line)  # Remove empty lines
    
    # Escape markdown chars for Telegram (do this AFTER HTML decoding)
    com = com.replace("*", "\\*").replace("_", "\\_").replace("[", "\\[").replace("]", "\\]")
    
    # Truncate if needed (respecting line breaks)
    if len(com) > max_len:
        com = com[:max_len].rsplit("\n", 1)[0] + "..."
    
    text = f"*No.{no}* — _{name}_\n{com}" if com else f"*No.{no}* — _{name}_"
    return text


def _news_menu_keyboard() -> dict:
    """Return news main menu keyboard."""
    return {
        "inline_keyboard": [
            [
                {"text": "📰 All Sources", "callback_data": "news:all"},
            ],
            [
                {"text": "🔍 Search by Source", "callback_data": "news:select"},
            ],
            [
                {"text": "⚙️ Configure Sources", "callback_data": "news:config_hint"},
            ],
        ]
    }


def _news_source_keyboard(sources: list) -> dict:
    """Build inline keyboard for news source selection."""
    buttons = []

    # "All Sources" button at the top
    buttons.append([{"text": "📰 All Sources", "callback_data": "news:all"}])

    # Individual source buttons (2 per row)
    row = []
    for i, source in enumerate(sources[:8], 1):  # Limit to 8 sources
        source_name = source.get("name", f"Source {i}")
        # Use short name for button
        short_name = source_name[:15] + "..." if len(source_name) > 15 else source_name
        row.append({"text": f"📄 {short_name}", "callback_data": f"news:source:{i}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    return {"inline_keyboard": buttons}


async def _send_news_source_selector(chat_id: str, sources: list):
    """Send news source selection menu."""
    # Cache sources for callback handling
    _news_source_cache[chat_id] = sources

    source_list = "\n".join([f"• {s.get('name', 'Unknown')}" for s in sources[:8]])
    text = f"📰 *Select a news source:*\n\n{source_list}"

    await telegram_service.send_message(
        chat_id,
        text,
        reply_markup=_news_source_keyboard(sources)
    )


async def _send_4chan_catalog(chat_id: str, board: str, user_id: int, offset: int = 0):
    """Fetch and display 4chan catalog for a board with thumbnails."""
    import asyncio
    from app.routers.fourchan import get_catalog

    result = await get_catalog(board=board)
    if "error" in result:
        await telegram_service.send_message(chat_id, f"❌ Error: {result['error']}")
        return

    threads = result.get("threads", [])
    if not threads:
        await telegram_service.send_message(chat_id, f"No threads found on /{board}/")
        return

    # Cache threads for this user (per board)
    if user_id not in _4chan_cache:
        _4chan_cache[user_id] = {}
    _4chan_cache[user_id][board] = threads

    threads_per_page = 10
    total_threads = len(threads)
    end_offset = min(offset + threads_per_page, total_threads)
    
    board_label = {"g": "🖥 Technology", "pol": "🌎 Politically Incorrect"}.get(board, board)

    # Send header with page info
    header = f"🍀 *4chan /{board}/ — {board_label}*"
    if offset > 0:
        header += f"\n📄 Page {offset // threads_per_page + 1} of {(total_threads - 1) // threads_per_page + 1}"
    header += f"\n\n*Showing threads {offset + 1}-{end_offset} of {total_threads}:*"
    await telegram_service.send_message(chat_id, header)

    # Get current page of threads
    page_threads = threads[offset:end_offset]

    # Send each thread with thumbnail (2 per message group for cleaner look)
    for i in range(0, len(page_threads), 2):
        # Get up to 2 threads
        thread_batch = page_threads[i:i+2]
        
        for j, t in enumerate(thread_batch, offset + i + 1):
            title = (t.get("title") or "No title")[:80]
            replies = t.get("replies", 0)
            images = t.get("images", 0)
            thread_id = t.get("thread_id")
            thumb_url = t.get("thumb_url")

            # Build caption
            caption = f"*{j}. {title}*\n💬 {replies} replies | 🖼 {images} images"
            caption = caption.replace("*", "\\*").replace("_", "\\_")
            caption = caption[:1000]  # Telegram caption limit

            # Build keyboard for this thread
            kbd = {
                "inline_keyboard": [[
                    {"text": "👁 View Thread", "callback_data": f"4c:thread:{board}:{thread_id}"}
                ]]
            }

            if thumb_url:
                # Send photo with caption and keyboard together
                photo_result = await telegram_service.send_photo(chat_id, thumb_url, caption, reply_markup=kbd)
                if not photo_result.get("ok"):
                    # Fallback to text only
                    text = f"{caption}\n[Thumbnail unavailable]"
                    await telegram_service.send_message(chat_id, text, reply_markup=kbd)
            else:
                # No thumbnail - send text with button
                await telegram_service.send_message(chat_id, caption, reply_markup=kbd)

            await asyncio.sleep(0.05)

    # Send board switcher with pagination at the end
    await telegram_service.send_message(
        chat_id,
        f"📋 Threads {offset + 1}-{end_offset} of {total_threads}",
        reply_markup=_4chan_board_switcher_keyboard(board, offset=offset, total_threads=total_threads)
    )


def _4chan_board_switcher_keyboard(current_board: str = "g", offset: int = 0, total_threads: int = 0) -> dict:
    """Return board switcher keyboard with pagination."""
    buttons = []
    threads_per_page = 10
    
    # First row: Pagination (Previous/Next)
    row1 = []
    if offset > 0:
        prev_offset = max(0, offset - threads_per_page)
        row1.append({"text": "⬅️ Previous", "callback_data": f"4c:catalogprev:{current_board}:{prev_offset}"})
    # Show Next if there are more threads
    remaining = total_threads - offset - threads_per_page
    if remaining > 0:
        next_offset = offset + threads_per_page
        row1.append({"text": "Next ➡️", "callback_data": f"4c:catalognext:{current_board}:{next_offset}"})
    if row1:
        buttons.append(row1)
    
    # Second row: Board switcher
    buttons.append([
        {"text": "🖥 /g/" if current_board != "g" else "✅ /g/", "callback_data": f"4c:board:g:0"},
        {"text": "🌎 /pol/" if current_board != "pol" else "✅ /pol/", "callback_data": f"4c:board:pol:0"},
    ])
    buttons.append([
        {"text": "🇯🇵 /a/" if current_board != "a" else "✅ /a/", "callback_data": f"4c:board:a:0"},
        {"text": "🔞 /h/" if current_board != "h" else "✅ /h/", "callback_data": f"4c:board:h:0"},
    ])
    
    return {"inline_keyboard": buttons}


async def _send_4chan_thread(chat_id: str, board: str, thread_id: int, user_id: int, summarize: bool = False, offset: int = 0):
    """Fetch and display a 4chan thread."""
    from app.routers.fourchan import get_thread, summarize_thread
    from app.database import SessionLocal

    if summarize:
        # Get AI summary
        db = SessionLocal()
        try:
            result = await summarize_thread(board=board, thread_id=thread_id, db=db, current_user=None)
            if "error" in result:
                await telegram_service.send_message(chat_id, f"❌ Error: {result['error']}")
                return

            summary = result.get("summary", "No summary available.")
            summary = summary.replace("*", "\\*").replace("_", "\\_")

            text = f"📝 *Thread Summary*\n\n_{summary[:3500]}_"
            if len(summary) > 3500:
                text += "..."

            await telegram_service.send_message(
                chat_id,
                text,
                reply_markup=_4chan_thread_keyboard(board, thread_id, has_summary=True)
            )
        finally:
            db.close()
        return

    # Get thread posts
    result = await get_thread(board=board, thread_id=thread_id)
    if "error" in result:
        await telegram_service.send_message(chat_id, f"❌ Error: {result['error']}")
        return

    posts = result.get("posts", [])
    if not posts:
        await telegram_service.send_message(chat_id, "No posts found in this thread.")
        return

    # Cache thread for navigation
    _4chan_thread_cache[chat_id] = {"board": board, "thread_id": thread_id, "posts": posts, "offset": offset}

    posts_per_page = 10
    total_posts = len(posts)
    
    # Calculate range of posts to show (OP is always shown, replies are paginated)
    # offset is the index in the replies (posts[1:])
    reply_start = 1 + offset
    reply_end = min(1 + offset + posts_per_page, total_posts)

    # Send thread header (OP post)
    op = posts[0]
    title = result.get("title", "Untitled Thread")
    title = title.replace("*", "\\*").replace("_", "\\_")

    header = f"🍀 *{title}*\n📋 /{board}/ — {total_posts} posts\n"
    if offset > 0:
        header += f"📄 Page {offset // posts_per_page + 1} of {(total_posts - 2) // posts_per_page + 1}\n"
    await telegram_service.send_message(chat_id, header)

    # Send OP post with image if available (only on first page)
    if offset == 0:
        op_text = _format_4chan_post(op, max_len=1000)
        op_image = op.get("image_url_direct") or op.get("image_url")

        if op_image:
            photo_result = await telegram_service.send_photo(chat_id, op_image, op_text)
            if not photo_result.get("ok"):
                await telegram_service.send_message(chat_id, op_text)
        else:
            await telegram_service.send_message(chat_id, op_text)

    # Send replies for current page
    for post in posts[reply_start:reply_end]:
        post_text = _format_4chan_post(post, max_len=600)
        post_image = post.get("image_url_direct") or post.get("image_url")

        if post_image:
            photo_result = await telegram_service.send_photo(chat_id, post_image, post_text)
            if not photo_result.get("ok"):
                await telegram_service.send_message(chat_id, post_text)
        else:
            await telegram_service.send_message(chat_id, post_text)

    # Calculate shown count and reply range
    reply_count = reply_end - reply_start
    shown_count = reply_count + (1 if offset == 0 else 0)  # Include OP on first page
    start_reply = offset + 1
    end_reply = offset + reply_count
    total_replies = total_posts - 1
    
    # Build status message
    if offset == 0:
        status_text = f"📖 OP + {reply_count} replies ({start_reply}-{end_reply} of {total_replies})"
    else:
        status_text = f"📖 Replies {start_reply}-{end_reply} of {total_replies}"
    
    # Send navigation keyboard
    await telegram_service.send_message(
        chat_id,
        status_text,
        reply_markup=_4chan_thread_keyboard(board, thread_id, offset=offset, total_posts=total_posts)
    )


def _help_main_keyboard() -> dict:
    """Inline keyboard for the help main menu."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔍 Search",      "callback_data": "prompt:search"},
                {"text": "🖼 Image Search", "callback_data": "prompt:images"},
            ],
            [
                {"text": "📎 Files",       "callback_data": "help:files"},
                {"text": "🎨 Image Gen",   "callback_data": "help:geni"},
            ],
            [
                {"text": "🧲 Torrents",    "callback_data": "t:menu"},
                {"text": "🎬 YouTube",     "callback_data": "help:youtube"},
            ],
            [
                {"text": "🎌 Nyaa",        "callback_data": "n:prompt"},
                {"text": "🍀 4chan",       "callback_data": "4c:select"},
            ],
            [
                {"text": "🌐 Translate",   "callback_data": "help:translate"},
                {"text": "📰 News",        "callback_data": "news:menu"},
            ],
            [
                {"text": "✉️ Email",       "callback_data": "help:mail"},
                {"text": "📱 Social Post", "callback_data": "help:post"},
            ],
            [
                {"text": "💬 Chat & URLs", "callback_data": "help:chat"},
                {"text": "📋 Logs",        "callback_data": "help:logs"},
            ],
            [
                {"text": "💰 Finance",     "callback_data": "help:finance"},
                {"text": "📸 Screenshot",  "callback_data": "prompt:screenshot"},
            ],
        ]
    }


_HELP_SECTIONS = {
    "finance": (
        "💰 *Finance — Budget Manager*\n\n"
        "Connect your account in the web UI \\(Settings → Finance\\), then send `/finance` "
        "\\(or tap 💰 Finance above\\)\\. It's all buttons — no commands to remember:\n\n"
        "• ✅ tap a bill to pay it\n"
        "• 📋 Unpaid / 📜 Paid / 📂 All — view your bills\n"
        "• ➕ Add Bill / 💵 Add Income — then reply with `name amount`\n"
        "• 🔄 Refresh — update the totals"
    ),
    "files": (
        "📎 *Files — compress, convert, meme, dildo, poo, cum, blood, fire & bulletholes*\n\n"
        "Just upload a file (no caption needed) and tap a button:\n\n"
        "*Images:*\n"
        "• 🗜 Compress — shrink the image\n"
        "• 📄 To PDF — combine your image(s) into one PDF\n"
        "• 🔤 Read text — OCR the text out of the image\n"
        "• ✨ Effects → 🖼 Meme — add outlined white caption text (I'll ask for it)\n"
        "• ✨ Effects → 🍆 Dildo — scatter dildos all over the image\n"
        "• ✨ Effects → 💩 Poo — scatter poop all over the image\n"
        "• ✨ Effects → 💦 Cum — scatter cum all over the image\n"
        "• ✨ Effects → 🩸 Blood — splatter blood all over the image\n"
        "• ✨ Effects → 🔥 Fire — set the image on fire\n"
        "• ✨ Effects → 🕳️ Bullet holes — punch bullet holes into the image\n"
        "• ✨ Effects → 🏳️‍🌈 Gay — stamp a big red GAY on the image\n"
        "• ✨ Effects → 🥷 Blacked — slap the BLACKED logo on the image\n"
        "• ✨ Effects → ✡️ Kosher — stamp a 100% KOSHER seal on the image\n"
        "• ✨ Effects → 🐶 Barked — drop a smirking dog + #BARKED on the image\n"
        "• ✨ Effects → 🎻 Hava — turn the image into a 6s Hava Nagila video\n"
        "• ✨ Effects → 🇮🇳 Indian — turn the image into a 6s Indian-song video\n"
        "• ✨ Effects → 🎷 Yakety — turn the image into a 9s Yakety Sax video\n"
        "• ✨ Effects → 🛑 Yamete — turn the image into a 6s yamete video\n"
        "• ✨ Effects → 😬 Curb — turn the image into a Curb Your Enthusiasm video\n"
        "• ✨ Effects → 😢 Depressing — turn the image into a 10s depressing video\n"
        "• ✨ Effects → 🌀 Fahh — turn the image into a fahh video\n"
        "• ✨ Effects → 🆘 Helpme — turn the image into a 5s helpme video\n"
        "• ✨ Effects → 🔔 Gong — turn the image into a gong video\n"
        "• ✨ Effects → 🚨 FBI — turn the image into an FBI open up video\n"
        "• ✨ Effects → 💳 Redeem — turn the image into a do not redeem video\n"
        "• ✨ Effects → 😏 Gigity — turn the image into a giggity video\n"
        "• ✨ Effects → 🤤 Beavis — turn the image into a Beavis laugh video\n"
        "• ✨ Effects → 👃 Smell — turn the image into a can you imagine the smell video\n"
        "• ✨ Effects → 🏚️ Hood — turn the image into a 10s hood video\n"
        "• ✨ Effects → 🕌 Akbar — turn the image into an akbar video\n"
        "• ✨ Effects → ⚠️ Retard — turn the image into a retard-alert video\n"
        "• ✨ Effects → 🤠 Whoabuddy — turn the image into a whoa buddy video\n"
        "• ✨ Effects → 🦅 Freebird — turn the image into a Free Bird video\n"
        "• ✨ Effects → 🐻 Kanye — turn the image into a Kanye video\n"
        "• ✨ Effects → 🌑 Darkness — turn the image into a darkness video\n"
        "• ✨ Effects → 🚲 Bike — turn the image into a bike video\n"
        "• ✨ Effects → 💼 Jobs — turn the image into a they-took-our-jobs video\n"
        "• ✨ Effects → 😡 Ree — turn the image into a REEEE video\n"
        "• ✨ Effects → 🗽 Liberal — turn the image into a liberal video\n"
        "• ✨ Effects → 📦 Moving — turn the image into a moving video\n"
        "• ✨ Effects → 🕺 Harlem — turn the image into a Harlem Shake video\n"
        "• ✨ Effects → 🐵 Chimp — overlay the animated chimp gif on the lower third\n"
        "• ✨ Effects → 🤔 Consider — overlay the 'consider the following' cutout\n"
        "• ✨ Effects → 🗣️ Clay — overlay the Clay Davis 'Shiiiit' clip (bg removed)\n"
        "• ✨ Effects → 🎸 Wasteland — turn the image into a Teenage Wasteland video\n"
        "• ✨ Effects → 🍑 Mixalot — turn the image into a Baby Got Back video\n"
        "• ✨ Effects → 😎 Thug — turn the image into a THUG LIFE video\n"
        "• 📣 Post to social — share it to your connected platforms\n\n"
        "*Video:*\n"
        "• 🗜 Compress — re-encode smaller (H.264, up to 1080p)\n"
        "• ✂️ Clip — trim to a start/end time (I'll ask for both)\n\n"
        "*PDF:*\n"
        "• 🖼 To images — one PNG per page\n"
        "• 📝 Summarize — AI summary of the document\n\n"
        "Tips:\n"
        "• Send several images, then tap *To PDF*, to merge them into one PDF.\n"
        "• You can also skip the buttons: send the file with `compress`, `clip 0:10 0:30`, `convert`, `meme <text>`, `dildo`, `poo`, `cum`, `blood`, `bullethole`, `fire`, `gay`, `blacked`, `kosher`, `blue`, `barked`, `hava`, `indian`, `yakety`, `yamete`, `curb`, `depressing`, `fahh`, `helpme`, `gong`, `fbi`, `redeem`, `gigity`, `beavis`, `smell`, `hood`, `akbar`, `retard`, `whoabuddy`, `robocop`, `titan`, `terminator`, `reze`, `sopranos`, `cheers`, `munsters`, `happydays`, `dontwanttowait`, `strangerthings`, `adamsfamily`, `xmen`, `futurama`, `charliesangles`, `differentstroke`, `seinfeld`, `onepiece`, `overtaken`, `freebird`, `kanye`, `darkness`, `bike`, `jobs`, `ree`, `liberal`, `moving`, `harlem`, `chimp`, `consider`, `clay`, `wasteland`, `mixalot`, `thug`, `feltedtables` or `feliz` as the caption.\n"
        "• Telegram limits bot downloads to 20 MB — use the web UI for bigger files."
    ),
    "youtube": (
        "🎬 *YouTube*\n\n"
        "Paste a YouTube link (or `yt <url>`) and choose:\n"
        "• 📋 Summary — AI summary of the video\n"
        "• 🎵 MP3 — download the audio\n"
        "• 🎬 Movie — download the video\n"
        "• 📣 Post — generate & share a social post\n\n"
        "Or use `ytdl <url>` for audio, `ytdl video <url>` for video.\n"
        "Trim and/or shrink a video in one go: `ytdl video <url> clip 0:10 0:30 compress`"
    ),
    "4chan": (
        "🍀 *4chan Browser*\n\n"
        "`4chan` — Select a board to browse\n"
        "`4chan g` — View /g/ (Technology) catalog\n"
        "`4chan pol` — View /pol/ catalog\n"
        "`4chan a` — View /a/ (Anime) catalog\n"
        "`4chan h` — View /h/ (Hentai) catalog\n\n"
        "*Features:*\n"
        "• Browse thread catalog with reply counts\n"
        "• Tap any thread to view posts with images\n"
        "• Summarize long threads with AI\n"
        "• Navigate with inline buttons\n"
        "• Open threads directly on 4chan"
    ),
    "chat": (
        "💬 *Chat & URLs*\n\n"
        "Just send any message to chat with the AI\\.\n\n"
        "• Reply to a message to use it as context\n"
        "• Send any URL to get a summary\n"
        "• Send a YouTube link for a video summary\n"
        "• Forward any article or link — auto\\-summarized\n"
        "• Send a photo to describe it or extract text \\(OCR\\)\n"
        "• The bot remembers recent conversation context"
    ),
    "search": (
        "🔍 *Web Search*\n\n"
        "`search <query>`\n"
        "Searches the web and returns an AI\\-written summary with source links\\.\n\n"
        "*Examples:*\n"
        "`search latest SpaceX launch`\n"
        "`search best Python frameworks 2025`"
    ),
    "images": (
        "🖼 *Image Search*\n\n"
        "`images <query>`\n"
        "Searches for images and sends them directly in the chat\\.\n\n"
        "*Examples:*\n"
        "`images northern lights`\n"
        "`images cyberpunk city art`"
    ),
    "translate": (
        "🌐 *Translation*\n\n"
        "`translate <text> to <language>`\n"
        "Translates text to any language\\.\n\n"
        "• Reply to any message with `translate` to translate it\n"
        "• Reply with `translate to Spanish` to specify the language\n"
        "• Send a photo with `translate` to OCR and translate the text in the image\n\n"
        "*Examples:*\n"
        "`translate hello world to Japanese`\n"
        "\\(reply to a message\\) `translate to French`"
    ),
    "news": (
        "📰 *News*\n\n"
        "`news` — Latest headlines from all sources\n"
        "`news <source>` — Headlines from a specific source\n\n"
        "*Examples:*\n"
        "`news`\n"
        "`news bbc`\n"
        "`news techcrunch`"
    ),
    "geni": (
        "🎨 *Image Generation*\n\n"
        "`geni <prompt>`\n"
        "Generates an image from your description using the configured AI backend\\.\n\n"
        "*Examples:*\n"
        "`geni a sunset over a cyberpunk city`\n"
        "`geni portrait of a samurai in watercolor style`"
    ),
    "torrents": (
        "🧲 *Torrents*\n\n"
        "`torrents` — Browse categories \\(Movies, TV, Music, Anime\\)\n"
        "`torrents search <query>` — Search by title\n"
        "`torrents list` — View & manage active downloads\n"
        "`torrents pause/resume/rm <#>` — Manage a download\n\n"
        "• Tap category buttons to browse top results\n"
        "• Each result has its own Download button\n"
        "• Send a magnet link directly to add it instantly\n\n"
        "*Examples:*\n"
        "`torrents search dark knight 1080p`\n"
        "`torrents list`"
    ),
    "nyaa": (
        "🎌 *Nyaa \\(Anime Torrents\\)*\n\n"
        "`nyaa <query>` — Search nyaa\\.si for anime torrents\n\n"
        "• Tap the *🔎 Nyaa Search* button and type your query when prompted\n"
        "• Each result has its own Download button\n\n"
        "*Examples:*\n"
        "`nyaa one piece 1080p`\n"
        "`nyaa attack on titan s4`"
    ),
    "mail": (
        "✉️ *Email*\n\n"
        "`mail <to> <body>`\n"
        "Sends an email using your configured mail settings\\.\n\n"
        "*Examples:*\n"
        "`mail alice@example.com Hey, just checking in\\!`"
    ),
    "post": (
        "📱 *Social Media Post Generator*\n\n"
        "Reply to any message \\(a bot answer, a link, a photo\\) and send a `post` command:\n\n"
        "• `post` — rewrite it into a viral, engaging post\n"
        "• `post raw` — share it *exactly as written*, no rewrite \\(also `verbatim`\\)\n"
        "• `post <instructions>` — rewrite it your way\n\n"
        "I then show share buttons for your connected platforms \\(Misskey / Pleroma / Matrix\\)\\.\n\n"
        "*Examples:*\n"
        "\\(reply to a good answer\\) `post raw`\n"
        "\\(reply to an article\\) `post professional`\n"
        "\\(reply to a link\\) `post don't include links`"
    ),
    "logs": (
        "📋 *System Logs*\n\n"
        "`logs` — Shows recent system log entries\\.\n"
        "Useful for checking errors or monitoring activity\\."
    ),
}


async def _send_torrent_results(chat_id: str, category: str, user_id: int):
    """Send each torrent result as its own message with a download button beneath it."""
    import asyncio
    from app.services.command_service import _torrent_cache

    cached = _torrent_cache.get(user_id, {}).get(category, [])
    if not cached:
        await telegram_service.send_message(chat_id, "No results found.")
        return

    cat_label = {"movies": "🎬 Movies", "tv": "📺 TV", "music": "🎵 Music", "anime": "🎌 Anime", "search": "🔍 Search"}.get(category, category.upper())
    await telegram_service.send_message(chat_id, f"**{cat_label}** — {len(cached)} results:")

    for i, t in enumerate(cached, 1):
        title = t.title[:80] + "..." if len(t.title) > 80 else t.title
        title_escaped = title.replace("[", "(").replace("]", ")")
        if t.url:
            title_line = f"[{title_escaped}]({t.url})"
        else:
            title_line = title_escaped
        text = f"**{i}. {title_line}**\n🌱 {t.seeders}  👤 {t.leechers}  📦 {t.size}"
        markup = {"inline_keyboard": [[
            {"text": f"📥 Download #{i}", "callback_data": f"t:dl:{category}:{i}"}
        ]]}
        await telegram_service.send_message(chat_id, text, reply_markup=markup)
        await asyncio.sleep(0.1)

    # Nav buttons at the end
    await telegram_service.send_message(chat_id, "Choose another category:", reply_markup=_torrent_nav_keyboard())


async def _send_nyaa_results(chat_id: str, user_id: int):
    """Send each nyaa result as its own message with a download button beneath it."""
    import asyncio
    from app.services.command_service import _nyaa_cache

    cached = _nyaa_cache.get(user_id, [])
    if not cached:
        await telegram_service.send_message(chat_id, "No results found.")
        return

    await telegram_service.send_message(chat_id, f"**🎌 Nyaa** — {len(cached)} results:")

    for i, t in enumerate(cached, 1):
        title = t.title[:80] + "..." if len(t.title) > 80 else t.title
        title_escaped = title.replace("[", "(").replace("]", ")")
        if t.url:
            title_line = f"[{title_escaped}]({t.url})"
        else:
            title_line = title_escaped
        text = f"**{i}. {title_line}**\n🌱 {t.seeders}  👤 {t.leechers}  📦 {t.size}"
        markup = {"inline_keyboard": [[
            {"text": f"📥 Download #{i}", "callback_data": f"n:dl:{i}"}
        ]]}
        await telegram_service.send_message(chat_id, text, reply_markup=markup)
        await asyncio.sleep(0.1)

    await telegram_service.send_message(chat_id, "Search again:", reply_markup={"inline_keyboard": [[
        {"text": "🔎 New Nyaa Search", "callback_data": "n:search_hint:0"},
        {"text": "📋 Active Downloads", "callback_data": "t:list:0"},
    ]]})


async def _send_active_torrents(chat_id: str, raw_content: str) -> None:
    """Send each active torrent as its own message with Pause/Remove buttons beneath it."""
    import asyncio

    refresh_btn = {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": "t:list:0"}]]}

    if not raw_content or raw_content.strip() == "No torrents.":
        await telegram_service.send_message(chat_id, "No active torrents.", reply_markup=refresh_btn)
        return

    # Separate any leading status message (e.g. "⏸️ Paused torrent #1") from the list
    header = ""
    list_content = raw_content
    if "**Torrents:**" in raw_content:
        pre, _, rest = raw_content.partition("**Torrents:**")
        header = pre.strip()
        list_content = "**Torrents:**" + rest
    else:
        # No torrent list — forward as-is with refresh button
        await telegram_service.send_message(chat_id, _strip_cmd_links(raw_content), reply_markup=refresh_btn)
        return

    if header:
        await telegram_service.send_message(chat_id, header)
        await asyncio.sleep(0.1)

    # Split into individual torrent blocks on lines starting with **N.
    blocks = re.split(r'\n(?=\*\*\d+\. )', list_content)
    torrent_blocks = [b for b in blocks if re.match(r'\*\*\d+\. ', b)]

    if not torrent_blocks:
        await telegram_service.send_message(chat_id, _strip_cmd_links(list_content), reply_markup=refresh_btn)
        return

    for block in torrent_blocks:
        num_match = re.match(r'\*\*(\d+)\. ', block)
        if not num_match:
            continue
        i = int(num_match.group(1))

        is_paused = f"cmd:torrents resume {i}" in block
        if is_paused:
            toggle = {"text": "▶ Resume", "callback_data": f"t:resume:{i}"}
        else:
            toggle = {"text": "⏸ Pause", "callback_data": f"t:pause:{i}"}
        remove = {"text": "🗑 Remove", "callback_data": f"t:rm:{i}"}

        markup = {"inline_keyboard": [[toggle, remove]]}
        await telegram_service.send_message(chat_id, _strip_cmd_links(block), reply_markup=markup)
        await asyncio.sleep(0.1)

    await telegram_service.send_message(
        chat_id,
        "Manage active downloads:",
        reply_markup=refresh_btn,
    )


router = APIRouter(prefix="/api/telegram", tags=["telegram"])

# Module-level update tracking to prevent duplicate processing across requests.
# A set of recently-seen update_ids handles restarts better than a single
# max-id (Telegram can re-deliver updates after downtime).
_seen_update_ids: set = set()
_MAX_SEEN_IDS = 500  # Keep a bounded window; Telegram won't replay further back

# Pending Misskey posts: chat_id → post_text (cleared once confirmed or cancelled)
_misskey_post_cache: dict = {}
# Pending Pleroma posts: chat_id → post_text
_pleroma_post_cache: dict = {}
# Pending Matrix posts: chat_id → post_text
_matrix_post_cache: dict = {}
# Matrix room selection cache: chat_id → list of {"room_id": str, "name": str}
_matrix_room_cache: dict = {}
# Sentinel stored in a cache after all:post consumes it, prevents double-post from old buttons
_CONSUMED = "__consumed__"
# Generated image bytes cache: chat_id → bytes (cleared with other share caches)
_geni_image_cache: dict = {}
# Pending link actions: chat_id → url (cleared once action is chosen)
_link_action_cache: dict = {}


async def _link_content_for_llm(db, url: str):
    """Fetch (title, content_or_None, error) for the summary/post LLM prompts.

    Goes through SearchService.fetch_urls, which already substitutes the transcript for YouTube
    links (so the model never summarizes contentless watch-page HTML). Callers must NOT generate
    from None content - that's how the hallucinated summaries/posts happened.
    """
    import asyncio as _asyncio
    from app.services.search_service import SearchService as _SS
    try:
        f = await _asyncio.wait_for(_SS(db).fetch_urls([url], max_urls=1), timeout=25)
    except _asyncio.TimeoutError:
        return "", None, "timed out fetching the URL"
    if f and f[0].get("content") and not f[0].get("error"):
        return f[0].get("title", ""), f[0]["content"], None
    return (f[0].get("title", "") if f else ""), None, (f[0].get("error") if f else "") or "could not fetch content"

# Finance: chat_id → {str(bill_id): bill_dict} for the bills shown in the last budget
# view, so a `fin:pay:<id>` callback can resolve the exact bill name to pay.
_finance_bills_cache: dict = {}

# ForceReply prompt for the "💵 Add Income" button. A reply to a message with this
# exact text is routed to `addbill <reply> income` (see the ForceReply router below).
_FIN_INCOME_PROMPT = "💵 Add income — reply: name amount"


async def _send_png_as_document(chat_id: str, image_b64: str, caption: str = None) -> bool:
    """Send a base64 PNG to a chat as a Telegram document. Returns True on success.

    Used as a fallback when send_photo rejects an image — Telegram caps photo
    dimensions/size, which full-page screenshots routinely exceed; documents don't.
    """
    try:
        import base64 as _b64
        png = image_b64
        if isinstance(png, str):
            if png.startswith("data:image"):
                png = png.split(",", 1)[1]
            png = _b64.b64decode(png)
        res = await telegram_service.send_document_bytes(chat_id, png, "image.png", caption, content_type="image/png")
        return bool(res.get("ok"))
    except Exception as e:
        logger.error(f"send_png_as_document failed: {e}")
        return False


async def _send_screenshot(chat_id: str, image_b64: str, caption: str) -> None:
    """Deliver a screenshot document-first (full resolution — Telegram compresses photos
    to an unreadable size for tall pages), falling back to a photo, then plain text.

    Then cache the full-res PNG and offer one-tap 🔤 Read text / 🌐 Translate buttons, so
    the user can OCR/translate the capture WITHOUT re-uploading it (a re-uploaded photo is
    compressed too small to read)."""
    sent = await _send_png_as_document(chat_id, image_b64, caption)
    if not sent:
        photo_result = await telegram_service.send_photo(chat_id, image_b64, caption)
        sent = photo_result.get("ok", False)
        if not sent:
            await telegram_service.send_message(chat_id, f"{caption}\n\n(Screenshot failed to send)")
            return

    try:
        import base64 as _b64, time as _t
        png = image_b64
        if isinstance(png, str):
            if png.startswith("data:image"):
                png = png.split(",", 1)[1]
            png = _b64.b64decode(png)
        _media_action_cache[chat_id] = {"attachments": [("screenshot.png", png, "image/png")], "ts": _t.time()}
        await telegram_service.send_message(
            chat_id,
            "Want the text? Tap below — reads the full-resolution capture (no re-upload needed):",
            reply_markup={"inline_keyboard": [[
                {"text": "🔤 Read text", "callback_data": "media:ocr"},
                {"text": "🌐 Translate", "callback_data": "media:translate"},
            ]]},
        )
    except Exception as _e:
        logger.warning(f"[screenshot] OCR/translate offer failed: {_e}")


async def _send_budget(chat_id: str, user, db, message_id: int = None) -> None:
    """Render the interactive budget view (summary + a Pay button per unpaid bill).

    When message_id is given the existing message is edited in place (used by the
    Pay / Refresh callbacks); otherwise a new message is sent.
    """
    from app.services import finance_service
    try:
        base, key = finance_service.get_config(db, user)
        summary = await finance_service.get_summary(base, key)
        bills = await finance_service.get_bills(base, key, status="unpaid")
    except finance_service.FinanceError as e:
        await telegram_service.send_message(chat_id, f"💰 {e}")
        return

    # Only expenses get a Pay button; income lines are informational.
    payable = [b for b in bills if not b.get("is_income")]
    _finance_bills_cache[chat_id] = {str(b["id"]): b for b in payable}

    # Live timestamp footer so each Refresh visibly changes the message — otherwise
    # tapping Refresh with unchanged data makes Telegram reject the edit with
    # "message is not modified" (400) and the button appears to do nothing.
    text = finance_service.format_summary(summary) + f"\n🕒 {datetime.now():%H:%M:%S}"
    rows = [
        [{"text": f"✅ {b['name'][:24]} ${abs(b.get('amount', 0)):,.0f}",
          "callback_data": f"fin:pay:{b['id']}"}]
        for b in payable[:20]
    ]
    rows.append([
        {"text": "📋 Unpaid", "callback_data": "fin:bills:unpaid"},
        {"text": "📜 Paid", "callback_data": "fin:bills:paid"},
        {"text": "📂 All", "callback_data": "fin:bills:all"},
    ])
    rows.append([
        {"text": "➕ Add Bill", "callback_data": "fin:add"},
        {"text": "💵 Add Income", "callback_data": "fin:addincome"},
    ])
    rows.append([
        {"text": "🔄 Refresh", "callback_data": "fin:refresh"},
    ])
    markup = {"inline_keyboard": rows}
    if message_id:
        await telegram_service.edit_message_text(chat_id, message_id, text, parse_mode="", reply_markup=markup)
    else:
        await telegram_service.send_message(chat_id, text, parse_mode="", reply_markup=markup)


async def _send_bills_list(chat_id: str, user, db, status: str, message_id: int = None) -> None:
    """Render a bills list (unpaid / paid / all) with a Back-to-Budget button.

    Driven by the `fin:bills:<status>` buttons on the budget view so the user never
    has to type the `bills` command. Edits in place when message_id is given.
    """
    from app.services import finance_service
    # The finance API filters with ?status=paid|unpaid; "all" means no filter.
    api_status = None if status == "all" else status
    header = {"paid": "Paid bills", "unpaid": "Unpaid bills", "all": "All bills"}.get(status, "Bills")
    try:
        base, key = finance_service.get_config(db, user)
        bills = await finance_service.get_bills(base, key, status=api_status)
    except finance_service.FinanceError as e:
        await telegram_service.send_message(chat_id, f"💰 {e}")
        return

    text = finance_service.format_bills(bills, header=header)
    markup = {"inline_keyboard": [[{"text": "⬅️ Back to Budget", "callback_data": "fin:refresh"}]]}
    if message_id:
        await telegram_service.edit_message_text(chat_id, message_id, text, parse_mode="", reply_markup=markup)
    else:
        await telegram_service.send_message(chat_id, text, parse_mode="", reply_markup=markup)


def _has_misskey(user) -> bool:
    return bool(
        user
        and getattr(user, "misskey_enabled", False)
        and getattr(user, "misskey_instance_url", None)
        and getattr(user, "misskey_api_token", None)
    )


def _has_pleroma(user) -> bool:
    return bool(
        user
        and getattr(user, "pleroma_enabled", False)
        and getattr(user, "pleroma_instance_url", None)
        and getattr(user, "pleroma_access_token", None)
    )


def _has_matrix(user) -> bool:
    return bool(
        user
        and getattr(user, "matrix_enabled", False)
        and getattr(user, "matrix_homeserver", None)
        and getattr(user, "matrix_access_token", None)
    )


def _strip_hashtags(text: str) -> str:
    """Remove hashtag tokens from AI-generated post text (the model often ignores
    the 'no hashtags' instruction). Apply BEFORE appending any URL so URL fragments
    like example.com#section are never touched."""
    import re as _ht
    text = _ht.sub(r'(?:^|\s)#\w[\w-]*', ' ', text)
    return _ht.sub(r'[ \t]{2,}', ' ', text).strip()


async def _offer_social_post(chat_id: str, post_text: str, user, telegram_svc, prompt: str = "📣 *Post this?*", image_bytes: Optional[bytes] = None):
    """Show the generated post and offer to share it on configured social platforms.

    Single source of truth for the per-chat image cache: callers that have an
    image to attach pass it as `image_bytes`; text-only callers (link summaries,
    news, yt) leave it None, which CLEARS any stale image left over from an
    earlier `geni`/photo share so it is never attached to an unrelated post.
    """
    if image_bytes is not None:
        _geni_image_cache[chat_id] = image_bytes
    else:
        _geni_image_cache.pop(chat_id, None)

    has_mk = _has_misskey(user)
    has_plr = _has_pleroma(user)
    has_mtx = _has_matrix(user)

    platform_count = sum([has_mk, has_plr, has_mtx])
    if platform_count == 0:
        # No platforms connected: echo the post text, but never send an EMPTY message
        # (Telegram rejects empty text). Image-only posts — e.g. a glowing text card —
        # have already delivered the image to the chat, so there's nothing more to say.
        if (post_text or "").strip():
            await telegram_svc.send_message(chat_id, post_text)
        return

    # Store post in all platform caches now so any button works
    if has_mk:
        _misskey_post_cache[chat_id] = post_text
    if has_plr:
        _pleroma_post_cache[chat_id] = post_text
    if has_mtx:
        _matrix_post_cache[chat_id] = post_text

    # Individual platform buttons on the first row
    individual = []
    if has_mk:
        individual.append({"text": "📣 Misskey", "callback_data": "mk:post"})
    if has_plr:
        individual.append({"text": "📣 Pleroma", "callback_data": "plr:post"})
    if has_mtx:
        individual.append({"text": "📣 Matrix", "callback_data": "mtx:post"})

    rows = [individual]

    # "Post to All" row — only shown when 2+ platforms configured
    if platform_count >= 2:
        rows.append([{"text": "🚀 Post to All", "callback_data": "all:post"}])

    # Glow it — render this text as a glowing neon graphic and attach it. Only for
    # text-only posts (when no image is already attached we'd otherwise clobber); once
    # glowed, image_bytes is set so the button won't reappear (no double-glow).
    if image_bytes is None and (post_text or "").strip():
        rows.append([{"text": "🌟 Glow it", "callback_data": "glow:textpost"}])

    rows.append([{"text": "❌ Skip", "callback_data": "mk:skip"}])

    await telegram_svc.send_message(
        chat_id,
        post_text + f"\n\n{prompt}",
        reply_markup={"inline_keyboard": rows},
    )
# Pending YouTube actions: chat_id → url (cleared once action is chosen)
_youtube_action_cache: dict = {}
# Pending uploaded media awaiting a button action: chat_id → {"attachments", "ts"}
_media_action_cache: dict = {}
_MEDIA_ACTION_TTL = 600  # seconds

# Interactive flashcards deck, per chat: chat_id → {"title", "cards", "idx", "answered"(list),
# "score", "ts"}. Ephemeral (no DB); expires after _FLASHCARD_TTL.
_flashcard_decks_cache: dict = {}
_FLASHCARD_TTL = 1800  # 30 min

# Interactive "✂️ Clip video" flow: after the user taps Clip we ForceReply for a
# start time, then an end time. State for the two-step prompt lives here, keyed by
# chat_id → {"start": float, "ts": float}; the source video stays in the media cache.
_clip_pending: dict = {}
# ForceReply prompt texts — matched verbatim to route the user's replies.
_CLIP_START_PROMPT = "✂️ Clip — reply with the START time (e.g. 0:10 or 90):"
_CLIP_END_PROMPT = "✂️ Clip — reply with the END time (e.g. 0:30 or 1:30):"
# After tapping "📣 Post to social" on an upload, ask for optional caption text
# before showing the platform buttons. Reply with "-" (or "skip") to post media only.
_SOCIAL_CAPTION_PROMPT = "✍️ Add a caption for your post? Reply with text, or send - to post without any."

# Every "share/post this?" prompt that gets appended to a post body or sent standalone
# alongside the platform buttons. A handler that recovers the post text from the button
# message (cache miss, e.g. after a restart) MUST strip these — and must never treat a
# bare prompt as the post body (bug: the prompt itself was posted to Misskey/Pleroma/
# Matrix on a caption-less media post). Keep in sync with every `prompt=` passed to
# _offer_social_post and every standalone "📣 …?" message.
_POST_PROMPTS = (
    "📣 *Post this?*", "📣 Post this?",
    "📣 *Post this (as written)?*", "📣 Post this (as written)?",
    "📣 Post this to your timeline?",
    "📣 *Share this image?*", "📣 Share this image?",
    "📣 *Share this?*", "📣 Share this?",
    "📣 *Post this glowing image?*", "📣 Post this glowing image?",
)


def _recover_post_text(callback_query: dict) -> str:
    """Recover the user's post text from the button message when the per-platform cache
    missed. Strips a trailing prompt; returns "" if the message was ONLY a prompt (a
    caption-less media post) so the prompt itself is never posted to the platform."""
    _msg_text = (callback_query.get("message") or {}).get("text", "") or ""
    for _prompt in _POST_PROMPTS:
        _suffix = "\n\n" + _prompt
        if _suffix in _msg_text:
            _msg_text = _msg_text[:_msg_text.rfind(_suffix)]
            break
    _msg_text = _msg_text.strip()
    if _msg_text in _POST_PROMPTS:
        return ""
    return _msg_text
# After tapping "🖼 Meme" on an image upload, ForceReply for the caption text; the
# source image stays in the media cache and is captioned when the reply arrives.
_MEME_PROMPT = "🖼 Meme — reply with the caption text to add:"
# After picking a motion then "✍️ Add text", ForceReply for the caption; the chosen
# effect + motion + the cached upload are remembered, and on the reply the effect
# renders with the motion and the caption burned on.
_EFFECT_CAPTION_PROMPT = "✍️ Reply with the caption text for this effect:"
# chat_id -> {"eff": str, "motion": str, "ts": float}. The motion is chosen first
# (mo: callback), then the optional caption is the FINAL step, so this carries the
# picked motion through the ForceReply caption round-trip.
_effect_caption_pending: dict = {}
# chat_id -> {"eff","motion","caption","ts"}. The character step is the FINAL one (after the optional
# caption); on the chosen character the effect renders ONCE with a combined arg
# "<motion> char <name> meme <caption>" so the shared CommandService parser does all the work.
_effect_char_pending: dict = {}


def _character_prompt_keyboard() -> dict:
    """Buttons to pick a bottom-right character (or skip). Drives the media:chr:<name> callback."""
    return {"inline_keyboard": [
        [{"text": "🧍 Schoolgirl", "callback_data": "media:chr:animegirl"},
         {"text": "🐸 Pepe", "callback_data": "media:chr:pepe"}],
        [{"text": "🇺🇸 Trump", "callback_data": "media:chr:trump"},
         {"text": "🐄 Cow", "callback_data": "media:chr:cow"}],
        [{"text": "🍈 Boobs", "callback_data": "media:chr:boobs"},
         {"text": "🩲 Panties", "callback_data": "media:chr:panties"}],
        [{"text": "▶️ No character", "callback_data": "media:chr:none"}],
    ]}


async def _deliver_files_result(chat_id: int, user, result: dict, offer_share: bool = True):
    """Send a CommandService 'files' result back as Telegram documents, optionally
    following up with a 'Post to social' prompt for the first image/video output.

    Module-level so both the callback handler and the message-handler caption reply
    deliver effect results identically (the callback's local `_send_files_result`
    delegates here)."""
    if result.get("type") == "files":
        if result.get("content"):
            await telegram_service.send_message(chat_id, result["content"])
        for f in result.get("files", []):
            if f.get("data"):
                await telegram_service.send_document_bytes(chat_id, f["data"], f.get("filename", "file"))
                await asyncio.sleep(0.15)
        if offer_share:
            _files = [f for f in result.get("files", []) if f.get("data")]
            _shareable = next(
                (f for f in _files if (f.get("content_type") or "").startswith(("image/", "video/"))),
                None,
            )
            if _shareable and (_has_misskey(user) or _has_pleroma(user) or _has_matrix(user)):
                _media_action_cache[chat_id] = {
                    "attachments": [(
                        _shareable.get("filename", "file"),
                        _shareable["data"],
                        _shareable.get("content_type", ""),
                    )],
                    "ts": time.time(),
                }
                await telegram_service.send_message(
                    chat_id, "📣 Post this to your timeline?",
                    reply_markup={"inline_keyboard": [[
                        {"text": "📣 Post to social", "callback_data": "media:post"},
                    ]]},
                )
    else:
        await telegram_service.send_message(chat_id, result.get("content", "Done."))


def _media_action_keyboard(attachments: list, user=None) -> Optional[dict]:
    """Build an inline keyboard offering actions for uploaded files.

    attachments is a list of (filename, data, content_type). Buttons depend on
    the file types present (image/video/pdf). If `user` has social platforms
    connected, an image upload also offers a Post button.
    """
    from app.services.media_service import is_image, is_video, is_pdf
    has_image = any(is_image(fn, ct) for fn, _, ct in attachments)
    has_video = any(is_video(fn, ct) for fn, _, ct in attachments)
    has_pdf = any(is_pdf(fn, ct) for fn, _, ct in attachments)
    has_doc = any((fn or "").lower().endswith((".pptx", ".docx", ".xlsx", ".ppt", ".doc"))
                  for fn, _, ct in attachments)

    _social = bool(user and (_has_misskey(user) or _has_pleroma(user) or _has_matrix(user)))
    rows = []
    if has_video:
        rows.append([
            {"text": "🗜 Compress video", "callback_data": "media:compress"},
            {"text": "✂️ Clip video", "callback_data": "media:clip"},
        ])
    if has_image:
        rows.append([
            {"text": "🗜 Compress", "callback_data": "media:compress"},
            {"text": "📄 To PDF", "callback_data": "media:topdf"},
        ])
        rows.append([
            {"text": "🔤 Read text", "callback_data": "media:ocr"},
            {"text": "🌐 Translate", "callback_data": "media:translate"},
        ])
        rows.append([
            {"text": "✨ Effects", "callback_data": "media:effects"},
        ])
    if has_pdf:
        rows.append([
            {"text": "🖼 To images", "callback_data": "media:toimg"},
            {"text": "📝 Summarize", "callback_data": "media:summarize"},
            {"text": "🌐 Translate", "callback_data": "media:translate"},
        ])
    # Study material (PDF / image / slide deck / doc) → interactive flashcards quiz.
    if has_pdf or has_image or has_doc:
        rows.append([{"text": "🎴 Flashcards", "callback_data": "media:fc"}])
    # Offer posting an image or video to connected social platforms.
    if _social and (has_image or has_video):
        rows.append([{"text": "📣 Post to social", "callback_data": "media:post"}])
    return {"inline_keyboard": rows} if rows else None


_FC_LETTERS = ["A", "B", "C", "D", "E"]


def _flashcard_keyboard(deck: dict) -> dict:
    """Inline keyboard for the current flashcard. Question face → one button per option
    (`fc:ans:<i>`); both faces → Prev/Next nav + Restart. State lives in the deck cache,
    so callback_data stays tiny."""
    from app.services.flashcards_service import _strip_latex
    idx = deck["idx"]; cards = deck["cards"]; total = len(cards)
    card = cards[idx]; answered = deck["answered"][idx]
    rows = []
    if answered is None:
        for i, opt in enumerate(card.get("options", [])):
            letter = _FC_LETTERS[i] if i < len(_FC_LETTERS) else "•"
            rows.append([{"text": f"{letter}. {_strip_latex(opt)}"[:60], "callback_data": f"fc:ans:{i}"}])
    nav = []
    if idx > 0:
        nav.append({"text": "◀ Prev", "callback_data": "fc:prev"})
    if idx < total - 1:
        nav.append({"text": "Next ▶", "callback_data": "fc:next"})
    if nav:
        rows.append(nav)
    rows.append([{"text": f"↻ Restart  ·  Score {deck.get('score', 0)}/{sum(1 for a in deck['answered'] if a is not None)}",
                  "callback_data": "fc:restart"}])
    return {"inline_keyboard": rows}


async def _send_flashcard(chat_id: str, deck: dict, message_id=None):
    """Render + send (new) or edit-in-place the deck's current card as a PNG."""
    import base64 as _b64
    from app.services import flashcards_service
    idx = deck["idx"]; total = len(deck["cards"]); card = deck["cards"][idx]
    answered = deck["answered"][idx]
    png = await asyncio.to_thread(
        flashcards_service.render_card_png, deck.get("title", "Flashcards"),
        idx, total, card, answered is not None, answered)
    kb = _flashcard_keyboard(deck)
    if message_id is None:
        res = await telegram_service.send_photo(chat_id, _b64.b64encode(png).decode(), None, reply_markup=kb)
        if res.get("ok"):
            deck["message_id"] = res.get("result", {}).get("message_id")
    else:
        await telegram_service.edit_message_media_photo(chat_id, message_id, png, reply_markup=kb)


# Effect catalog grouped into the three Effects categories. Each entry is
# (button label, effect name → media:zq:<name>). Adding a new effect = add ONE
# line to the right group; the keyboards below build themselves. "meme" is special
# (direct caption flow via media:meme) and is prepended to the Memes keyboard.
_FX_THEMES = [
    ("🇮🇹 Sopranos", "sopranos"), ("🍻 Cheers", "cheers"),
    ("🧛 Munsters", "munsters"), ("😃 Happy Days", "happydays"),
    ("🌊 Don't Wait", "dontwanttowait"), ("🔦 Stranger Things", "strangerthings"),
    ("🖤 Addams Family", "adamsfamily"), ("❌ X-Men", "xmen"),
    ("🚀 Futurama", "futurama"), ("👼 Charlie's Angels", "charliesangles"),
    ("🌍 Diff'rent Strokes", "differentstroke"), ("🎤 Seinfeld", "seinfeld"),
    ("🦅 Freebird", "freebird"), ("🕺 Harlem", "harlem"),
    ("🎻 Hava", "hava"), ("🎷 Yakety", "yakety"),
    ("😬 Curb", "curb"), ("🎸 Wasteland", "wasteland"),
    ("🍑 Mixalot", "mixalot"), ("🏴‍☠️ One Piece", "onepiece"),
    ("🤖 Robocop", "robocop"), ("🗿 Titan", "titan"),
    ("🦾 Terminator", "terminator"), ("💣 Reze", "reze"),
]
_FX_SOUNDS = [
    ("🤠 Whoabuddy", "whoabuddy"), ("🕌 Akbar", "akbar"),
    ("⚠️ Retard", "retard"), ("🔔 Gong", "gong"),
    ("🚨 FBI", "fbi"), ("💳 Redeem", "redeem"),
    ("😏 Gigity", "gigity"), ("🤤 Beavis", "beavis"),
    ("👃 Smell", "smell"), ("🏚️ Hood", "hood"),
    ("🇮🇳 Indian", "indian"), ("🛑 Yamete", "yamete"),
    ("😢 Depressing", "depressing"), ("🌀 Fahh", "fahh"),
    ("🆘 Helpme", "helpme"), ("🐶 Barked", "barked"),
    ("😡 Ree", "ree"), ("🐻 Kanye", "kanye"),
    ("🌑 Darkness", "darkness"), ("🚲 Bike", "bike"),
    ("💼 Jobs", "jobs"), ("🗽 Liberal", "liberal"),
    ("📦 Moving", "moving"), ("🏎️ Overtaken", "overtaken"),
    ("🎱 Felted Tables", "feltedtables"), ("🙏 Prayer", "prayer"),
    ("🎉 Feliz", "feliz"),
]
_FX_MEMES = [
    ("🍆 Dildo", "dildo"), ("💩 Poo", "poo"),
    ("💦 Cum", "cum"), ("🩸 Blood", "blood"),
    ("🔥 Fire", "fire"), ("🕳️ Bullet holes", "bullethole"),
    ("🏳️‍🌈 Gay", "gay"), ("🥷 Blacked", "blacked"),
    ("✡️ Kosher", "kosher"), ("🤔 Consider", "consider"),
    ("🐵 Chimp", "chimp"), ("🗣️ Clay", "clay"),
    ("😎 Thug", "thug"), ("🔵 Blue", "blue"),
]


def _fx_category_keyboard(effects: list, back_to: str) -> dict:
    """Build an effect sub-keyboard: buttons (2 per row) + a Back button.
    `back_to` is the callback the Back button fires (the category picker)."""
    rows: list = []
    pair: list = []
    for lbl, name in effects:
        pair.append({"text": lbl, "callback_data": f"media:zq:{name}"})
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([{"text": "⬅️ Back", "callback_data": back_to}])
    return {"inline_keyboard": rows}


def _media_effects_keyboard() -> dict:
    """Category picker shown after tapping '✨ Effects' on an upload. Splits the
    (50+) effects into Themes / Sounds / Memes so no single list is overwhelming;
    each opens its own sub-keyboard (media:fxcat:<cat>)."""
    return {"inline_keyboard": [
        [
            {"text": "📺 TV/Movie Themes", "callback_data": "media:fxcat:themes"},
            {"text": "🔊 Sound clips", "callback_data": "media:fxcat:sounds"},
        ],
        [
            {"text": "🎨 Memes / overlays", "callback_data": "media:fxcat:memes"},
        ],
        [
            {"text": "🪄 Alive (3D)", "callback_data": "media:alive"},
            {"text": "🌟 Glow", "callback_data": "media:glow"},
        ],
        [{"text": "⬅️ Back", "callback_data": "media:back"}],
    ]}


def _media_fx_themes_keyboard() -> dict:
    return _fx_category_keyboard(_FX_THEMES, "media:effects")


def _media_fx_sounds_keyboard() -> dict:
    return _fx_category_keyboard(_FX_SOUNDS, "media:effects")


def _media_fx_memes_keyboard() -> dict:
    # Meme has its own caption flow (media:meme), so prepend it as a button.
    kb = _fx_category_keyboard(_FX_MEMES, "media:effects")
    kb["inline_keyboard"].insert(0, [{"text": "🖼 Meme", "callback_data": "media:meme"}])
    return kb


def _ytdl_video_keyboard() -> dict:
    """Action buttons shown after `ytdl video <url>` downloads a video: send it
    as-is, or trim/shrink it first. Compress/Clip reuse the standard media-action
    callbacks; 'Clip + Compress' runs the clip flow then compresses the result."""
    return {"inline_keyboard": [
        [{"text": "📤 Send as-is", "callback_data": "ytdlv:send"}],
        [
            {"text": "🗜 Compress", "callback_data": "media:compress"},
            {"text": "✂️ Clip", "callback_data": "media:clip"},
        ],
        [{"text": "🗜✂️ Clip + Compress", "callback_data": "media:clipcompress"}],
    ]}


async def _offer_ytdl_video_actions(chat_id: str, dl_result, source_url: str, user, db) -> None:
    """After a video download, cache it and offer Send / Compress / Clip / Clip+Compress.

    Shared by the `ytdl video` command and the pasted-link 🎬 Movie button so both
    let the user trim/shrink before sending (a 100-min video need not be sent whole).
    Above a sane in-RAM cap the file is saved to storage instead (clip/compress on a
    file that large isn't worth holding in memory). The temp dir is the caller's to
    clean — we read the bytes into the cache here so it survives that cleanup.
    """
    import os as _os
    _raw = _os.path.getsize(dl_result.local_path)
    if _raw > 250 * 1024 * 1024:
        from app.services.youtube_service import (
            download_video_and_save_to_storage, format_download_result,
        )
        save_result = await download_video_and_save_to_storage(
            url=source_url, user_id=user.id, db=db, subfolder="YouTube Videos",
        )
        await telegram_service.send_message(
            chat_id,
            f"❌ Video is too large to process here ({_raw // (1024*1024)} MB).\n\n{format_download_result(save_result)}"
        )
        return
    _fn = _os.path.basename(dl_result.local_path)
    with open(dl_result.local_path, "rb") as _vf:
        _vbytes = _vf.read()
    _cap = f"🎬 {dl_result.title}" if dl_result.title else "🎬 Video"
    _media_action_cache[chat_id] = {
        "attachments": [(_fn, _vbytes, "video/mp4")],
        "ts": time.time(),
        "ytdl": {"caption": _cap, "duration": int(dl_result.duration) if dl_result.duration else None},
    }
    await telegram_service.send_message(
        chat_id,
        f"✅ Downloaded ({_raw // (1024*1024)} MB). Send it as-is, or trim/shrink it first?",
        reply_markup=_ytdl_video_keyboard(),
    )


async def _offer_ytdl_share(chat_id: str, filename: str, video_bytes: bytes, db) -> None:
    """After a ytdl video is delivered (as-is or trimmed/compressed), offer to post
    it to the user's connected social platforms. No-op if none are connected.

    Points the media-action cache at the *delivered* bytes so 'Post to social'
    shares exactly what the user just received (e.g. the trimmed clip), then reuses
    the standard `media:post` flow.
    """
    user = db.query(User).filter(
        User.telegram_chat_id == chat_id, User.telegram_enabled == True
    ).first()
    if not (user and (_has_misskey(user) or _has_pleroma(user) or _has_matrix(user))):
        return
    _media_action_cache[chat_id] = {
        "attachments": [(filename or "video.mp4", video_bytes, "video/mp4")],
        "ts": time.time(),
    }
    await telegram_service.send_message(
        chat_id, "📣 Post this to your timeline?",
        reply_markup={"inline_keyboard": [[{"text": "📣 Post to social", "callback_data": "media:post"}]]},
    )


# Languages offered when translating an uploaded image/PDF.
_TRANSLATE_LANGS = [
    "English", "Spanish", "French",
    "German", "Italian", "Portuguese",
    "Russian", "Chinese", "Japanese",
    "Korean", "Arabic", "Thai",
]


def _media_translate_keyboard() -> dict:
    """Language picker shown after the Translate button on an upload."""
    rows, row = [], []
    for lang in _TRANSLATE_LANGS:
        row.append({"text": lang, "callback_data": f"media:tr:{lang.lower()}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


# Pending news Post actions: chat_id → list of (title, url) tuples
_news_post_cache: dict = {}
# News source cache: chat_id → list of news sources
_news_source_cache: dict = {}
# 4chan cache: user_id → {board: [threads]}
_4chan_cache: dict = {}
# 4chan thread cache: chat_id → {board, thread_id, posts}
_4chan_thread_cache: dict = {}


class TelegramWebhookUpdate(BaseModel):
    update_id: int
    message: Optional[dict] = None
    callback_query: Optional[dict] = None
    edited_message: Optional[dict] = None
    inline_query: Optional[dict] = None
    chosen_inline_result: Optional[dict] = None


# Allow any incoming dict for the webhook
class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[dict] = None
    callback_query: Optional[dict] = None
    edited_message: Optional[dict] = None
    inline_query: Optional[dict] = None
    chosen_inline_result: Optional[dict] = None
    my_chat_member: Optional[dict] = None
    chat_member: Optional[dict] = None
    
    class Config:
        extra = "allow"


class TelegramBotConfig(BaseModel):
    bot_token: Optional[str] = None
    webhook_url: Optional[str] = None
    enabled: bool = False


class TelegramChatSetup(BaseModel):
    chat_id: str
    notifications: str = "news,downloads,mentions"


@router.get("/me")
async def get_bot_info(db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    """Get information about the configured bot."""
    bot_token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
    if not bot_token or not bot_token.value:
        raise HTTPException(status_code=400, detail="Telegram bot not configured")
    
    result = await telegram_service.get_me()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to get bot info"))
    
    return result.get("result", {})


@router.post("/webhook")
async def telegram_webhook(update: dict, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Handle incoming webhook updates from Telegram.

    Returns 200 OK immediately so Telegram doesn't time out (60s limit),
    then processes the message in a background task.
    """
    global _seen_update_ids
    update_id = update.get("update_id", 0)
    if update_id in _seen_update_ids:
        logger.info(f"Skipping duplicate update_id: {update_id}")
        return {"ok": True}
    _seen_update_ids.add(update_id)
    if len(_seen_update_ids) > _MAX_SEEN_IDS:
        # Trim oldest entries — update_ids are monotonically increasing
        _seen_update_ids = set(sorted(_seen_update_ids)[-_MAX_SEEN_IDS:])

    # Acknowledge immediately — processing may take longer than Telegram's 60s timeout
    background_tasks.add_task(_process_telegram_update, update)
    return {"ok": True}


async def _process_telegram_update(update: dict):
    """Process a Telegram update in the background with its own DB session."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        await _handle_telegram_update(update, db)
    except Exception as e:
        logger.error(f"Background Telegram processing error: {e}", exc_info=True)
    finally:
        db.close()


async def _handle_telegram_update(update: dict, db: Session):
    """Core Telegram update processing logic."""
    logger.info(f"Received Telegram webhook update: {update}")
    try:
        from app.services.chat_service import ChatService
        
        bot_token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
        if not bot_token or not bot_token.value:
            logger.warning("Telegram bot not configured")
            return {"ok": False, "error": "Bot not configured"}
        
        telegram_service.set_token(bot_token.value)
        # Point at a local Bot API server if the admin enabled one (lifts the
        # 20 MB download cap to ~2 GB); otherwise use the cloud API.
        _configure_telegram(db)

        message = update.get("message")
        logger.warning(f"TELEGRAM WEBHOOK: Received update")
        
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
                "🔎 Type your anime search:": "nyaa",
                "🔍 Type your torrent search:": "torrents search",
                "🔍 What would you like to search for?": "search",
                "🖼 What images would you like to search for?": "images",
                "🎨 Describe the image you want to generate:": "geni",
                "📸 Send the URL to screenshot:": "screenshot",
                "💰 Add a bill — reply: name amount": "addbill",
            }
            reply_from = (reply_to or {}).get("from", {})
            if reply_from.get("is_bot") and text.strip():
                route = _FORCE_REPLY_ROUTES.get(reply_text.strip())
                if route:
                    text = f"{route} {text.strip()}"
                    text_lower = text.lower()
                    reply_to = {}
                    reply_text = ""
                elif reply_text.strip() == _FIN_INCOME_PROMPT:
                    # "💵 Add Income" button → reuse addbill with the income flag appended.
                    text = f"addbill {text.strip()} income"
                    text_lower = text.lower()
                    reply_to = {}
                    reply_text = ""

            # Reply to the "Post to social" caption prompt → attach the cached media
            # and show the platform buttons with the user's caption (or none if "-").
            if reply_from.get("is_bot") and reply_text.strip() == _SOCIAL_CAPTION_PROMPT:
                from app.services.media_service import is_image, is_video
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the file again.")
                    return {"ok": True}
                _atts = _entry["attachments"]
                _media = next((fd for fn, fd, ct in _atts if is_image(fn, ct)), None) \
                    or next((fd for fn, fd, ct in _atts if is_video(fn, ct)), None)
                if not _media:
                    await telegram_service.send_message(chat_id, "Nothing to post.")
                    return {"ok": True}
                _cap_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not _cap_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                _caption = text.strip()
                if _caption in ("-", "skip"):
                    _caption = ""
                await _offer_social_post(
                    chat_id, _caption, _cap_user, telegram_service,
                    prompt="📣 *Post this?*", image_bytes=_media,
                )
                return {"ok": True}

            # Reply to the "🖼 Meme" caption prompt → caption the cached image.
            if reply_from.get("is_bot") and reply_text.strip() == _MEME_PROMPT:
                from app.services.media_service import is_image
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the image again.")
                    return {"ok": True}
                _caption = text.strip()
                if not _caption:
                    await telegram_service.send_message(
                        chat_id, "⚠️ Empty caption. " + _MEME_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "TOP TEXT"},
                    )
                    return {"ok": True}
                _meme_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not _meme_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                _atts = [a for a in _entry["attachments"] if is_image(a[0], a[2])]
                await telegram_service.send_message(chat_id, "🖼 Adding caption…")
                try:
                    _res = await CommandService(db, user=_meme_user).execute_command(
                        "meme", _caption, attachments=_atts
                    )
                    if _res.get("type") == "files":
                        # Send as a document (not send_photo): the meme is a JPEG, whose
                        # base64 starts with "/9j/" — send_photo would treat that as a
                        # file path and fail. send_document_bytes takes raw bytes.
                        for _f in _res.get("files", []):
                            if _f.get("data"):
                                await telegram_service.send_document_bytes(chat_id, _f["data"], _f.get("filename", "meme.jpg"))
                                await asyncio.sleep(0.15)
                    else:
                        await telegram_service.send_message(chat_id, _res.get("content", "Done."))
                except Exception as _meme_err:
                    logger.error(f"Meme failed: {_meme_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Meme failed: {_meme_err}")
                return {"ok": True}

            # Reply to the effect caption prompt → the motion was already chosen (held in
            # _effect_caption_pending["motion"]); render the effect with motion + caption.
            if reply_from.get("is_bot") and reply_text.strip() == _EFFECT_CAPTION_PROMPT:
                from app.services.media_service import is_image
                _pend = _effect_caption_pending.get(chat_id)
                _entry = _media_action_cache.get(chat_id)
                if not _pend or not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _effect_caption_pending.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the image again.")
                    return {"ok": True}
                _cap = text.strip()
                if not _cap:
                    await telegram_service.send_message(
                        chat_id, "⚠️ Empty caption. " + _EFFECT_CAPTION_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "TOP TEXT"},
                    )
                    return {"ok": True}
                _eff = _pend["eff"]
                _motion = _pend.get("motion", "")
                _cap_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not _cap_user:
                    _effect_caption_pending.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                # Caption captured — the character step is the FINAL one; render happens there.
                _effect_char_pending[chat_id] = {"eff": _eff, "motion": _motion, "caption": _cap, "ts": time.time()}
                _effect_caption_pending.pop(chat_id, None)
                await telegram_service.send_message(
                    chat_id, "🧸 Add a character (bottom-right)?",
                    reply_markup=_character_prompt_keyboard(),
                )
                return {"ok": True}

            # Interactive video-clip flow: replies to the start/end ForceReply prompts.
            # Handled here (before social-reply/command routing) since it spans two
            # steps and pulls the source video from the media-action cache.
            if reply_from.get("is_bot") and reply_text.strip() in (_CLIP_START_PROMPT, _CLIP_END_PROMPT):
                from app.services.media_service import parse_timecode, clip_attachment, is_video
                _val = parse_timecode(text.strip())
                if reply_text.strip() == _CLIP_START_PROMPT:
                    if _val is None:
                        await telegram_service.send_message(
                            chat_id, "⚠️ Couldn't read that time. " + _CLIP_START_PROMPT,
                            reply_markup={"force_reply": True, "selective": True,
                                          "input_field_placeholder": "0:10"},
                        )
                        return {"ok": True}
                    _clip_pending[chat_id] = {"start": _val, "ts": time.time()}
                    await telegram_service.send_message(
                        chat_id, _CLIP_END_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "0:30"},
                    )
                    return {"ok": True}

                # End-time reply → validate against the stored start, then clip.
                if _val is None:
                    await telegram_service.send_message(
                        chat_id, "⚠️ Couldn't read that time. " + _CLIP_END_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "0:30"},
                    )
                    return {"ok": True}
                _pending = _clip_pending.get(chat_id)
                if not _pending or (time.time() - _pending.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _clip_pending.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That clip request expired — tap ✂️ Clip video again.")
                    return {"ok": True}
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    _clip_pending.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the video again.")
                    return {"ok": True}
                _start = _pending["start"]
                if _val <= _start:
                    await telegram_service.send_message(
                        chat_id, "⚠️ The end time must be after the start. " + _CLIP_END_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "0:30"},
                    )
                    return {"ok": True}
                _clip_pending.pop(chat_id, None)
                _compress_after = bool(_entry.get("compress_after"))
                _is_ytdl = bool(_entry.get("ytdl"))  # offer a share prompt afterwards
                _entry.pop("compress_after", None)
                _atts = [a for a in _entry["attachments"] if is_video(a[0], a[2])]
                await telegram_service.send_message(chat_id, "✂️ Clipping…" + (" then compressing…" if _compress_after else ""))
                try:
                    _outs, _summary = await asyncio.to_thread(clip_attachment, _atts, _start, _val)
                    if not _outs:
                        await telegram_service.send_message(chat_id, _summary)
                    else:
                        # Optionally compress the clipped result (the "Clip + Compress" action).
                        if _compress_after:
                            from app.services.media_service import compress_attachments
                            _catts = [(f["filename"], f["data"], f["content_type"]) for f in _outs if f.get("data")]
                            _couts, _csummary = await asyncio.to_thread(compress_attachments, _catts)
                            if _couts:
                                _outs, _summary = _couts, f"{_summary}\n{_csummary}"
                        await telegram_service.send_message(chat_id, _summary)
                        for _f in _outs:
                            if _f.get("data"):
                                await telegram_service.send_document_bytes(chat_id, _f["data"], _f.get("filename", "clip.mp4"))
                                await asyncio.sleep(0.15)
                        # For a ytdl download, offer to post the trimmed/compressed result.
                        if _is_ytdl and _outs[0].get("data"):
                            await _offer_ytdl_share(chat_id, _outs[0].get("filename", "clip.mp4"), _outs[0]["data"], db)
                except Exception as _clip_err:
                    logger.error(f"Clip failed: {_clip_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Clip failed: {_clip_err}")
                return {"ok": True}

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
            
            logger.warning(f"TELEGRAM: text='{text}', reply_to='{reply_text[:50] if reply_text else ''}', photos={len(photos) if photos else 0}")
            
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
            commands = ["help", "new", "ytdl", "geni", "mail", "news", "search", "images", "yt", "torrents", "nyaa", "4chan", "logs", "translate", "post", "share", "compress", "clip", "convert", "flashcards", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "alive", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "robocop", "titan", "terminator", "reze", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz", "node", "budget", "finance", "bills", "pay", "addbill", "screenshot", "shot", "ss"]
            for cmd in commands:
                if text_lower.startswith(cmd + " ") or text_lower == cmd:
                    command = cmd
                    arg = text[len(cmd):].strip()
                    break
            # Telegram skips parse_command, so resolve aliases (e.g. shot/ss -> screenshot) to the
            # canonical name, or execute_command rejects them as "Unknown command".
            if command:
                command = CommandService.COMMAND_ALIASES.get(command, command)

            # "post" can appear anywhere in a short reply message (e.g. "send post", "make a post")
            if command is None and reply_to and len(text_lower.split()) <= 5 and "post" in text_lower:
                command = "post"
                # Only use words AFTER "post" as tone modifier (e.g. "post professional" → "professional")
                parts = text_lower.split("post", 1)
                arg = parts[1].strip() if len(parts) > 1 else ""

            # If it's a reply and translate command, handle it
            if reply_text and command == "translate":
                logger.warning(f"TRANSLATE: Processing reply with text: {reply_text[:100]}...")
                # Use the replied text for translation. Language = 1-2 words after an optional
                # leading "to", dropping any trailing instruction ("... and explain"). (Plain
                # arg.replace("to","") mangled words like "Esperanto".)
                _lm = re.match(r'^(?:to\s+)?([A-Za-z][A-Za-z\- ]*?)(?:\s+and\s+.*)?$',
                               arg.strip(), re.IGNORECASE)
                language = (_lm.group(1).strip().title() if _lm and _lm.group(1).strip() else "English")
                
                from app.services.chat_service import ChatService as FreshChatService
                fresh_chat_service = FreshChatService(db, user=None)
                
                translate_messages = [
                    {"role": "system", "content": f"Translate the following text to {language}. Output ONLY the translation, nothing else. Do NOT add any commentary, emojis, or persona."},
                    {"role": "user", "content": reply_text}
                ]
                
                try:
                    translated = await fresh_chat_service.chat(translate_messages)
                    logger.warning(f"TRANSLATE: Got translation: {translated[:100]}...")
                    result = {"type": "text", "content": f"## Translation to {language}\n\n{translated}"}
                except Exception as e:
                    logger.error(f"Translation error: {e}")
                    result = {"type": "text", "content": f"Translation failed: {str(e)}"}
                
                await telegram_service.send_message(chat_id, result.get("content", ""))
                logger.warning(f"TRANSLATE: Sent translation result")
                return {"ok": True}
            
            # post command: generate a social media post from a replied-to link
            if command == "post":
                if not reply_to and not text:
                    await telegram_service.send_message(chat_id, "Reply to a message and send `post` to generate a social media post, or `post raw` to share it exactly as written. You can add instructions too, e.g. `post professional` or `post don't include links`.")
                    return {"ok": True}

                # `post raw` / `post verbatim` shares the replied-to text AS-IS,
                # skipping the LLM rewrite. The keyword is consumed so it isn't
                # mistaken for a tone modifier (e.g. `post professional`).
                _arg_l = arg.strip().lower()
                verbatim = False
                _inline_after = ""
                for _kw in ("verbatim", "as-is", "as is", "asis", "raw", "exact", "exactly"):
                    if _arg_l == _kw or _arg_l.startswith(_kw + " "):
                        verbatim = True
                        _inline_after = arg.strip()[len(_kw):].strip()
                        break

                import re as _re

                # Extract URL from replied-to message — check text, entities, and caption
                def _extract_url_from_msg(msg: dict) -> str:
                    # 1. Raw URL in text
                    for field in ("text", "caption"):
                        val = msg.get(field, "") or ""
                        found = _re.findall(r'https?://\S+', val)
                        if found:
                            return found[0].rstrip('.,)')
                    # 2. URL entity (Telegram stores link-preview URLs here)
                    for entity_field in ("entities", "caption_entities"):
                        for ent in msg.get(entity_field, []) or []:
                            if ent.get("type") in ("url", "text_link"):
                                url = ent.get("url") or ""
                                if url.startswith("http"):
                                    return url.rstrip('.,)')
                    # 3. Link preview metadata
                    web = msg.get("web_page") or msg.get("link_preview") or {}
                    if web.get("url"):
                        return web["url"].rstrip('.,)')
                    return ""

                url_to_append = _extract_url_from_msg(reply_to or {}) or _extract_url_from_msg(message)
                source_text = reply_text or url_to_append or text
                logger.info(f"post command: url={url_to_append!r}, source_text={source_text[:80] if source_text else ''}...")

                # If the reply contains a photo but no URL, share the image directly
                # instead of generating an AI post with no real content
                _rt_photos = (reply_to or {}).get("photo", []) or message.get("photo", [])
                if not url_to_append and _rt_photos:
                    _rt_file_id = _rt_photos[-1].get("file_id")
                    if _rt_file_id:
                        _rt_fr = await telegram_service.get_file(_rt_file_id)
                        if _rt_fr and _rt_fr.get("ok"):
                            _rt_fp = _rt_fr.get("result", {}).get("file_path")
                            if _rt_fp:
                                _rt_data = await telegram_service.download_file(_rt_fp)
                                if _rt_data:
                                    # In verbatim mode `arg` is just the keyword ("raw"); don't
                                    # let it become the image caption — fall back to the reply text.
                                    _cap_arg = "" if verbatim else arg.strip()
                                    _share_caption = _cap_arg or reply_text or "Image"
                                    _tg_user_share = db.query(User).filter(
                                        User.telegram_chat_id == chat_id,
                                        User.telegram_enabled == True
                                    ).first()
                                    await _offer_social_post(chat_id, _share_caption, _tg_user_share,
                                                             telegram_service, prompt="📣 *Share this image?*",
                                                             image_bytes=_rt_data)
                                    return {"ok": True}

                # Verbatim mode: share the reply text exactly as written, no LLM rewrite.
                if verbatim:
                    raw_text = (reply_text or _inline_after).strip()
                    if not raw_text:
                        await telegram_service.send_message(chat_id, "Nothing to post — reply to a message with text and send `post raw`.")
                        return {"ok": True}
                    # Append the source URL if the reply references one but doesn't already include it.
                    if url_to_append and url_to_append not in raw_text:
                        raw_text = raw_text.rstrip() + f"\n\n{url_to_append}"
                    _tg_user_raw = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()
                    await _offer_social_post(
                        chat_id, raw_text, _tg_user_raw, telegram_service,
                        prompt="📣 *Post this (as written)?*"
                    )
                    return {"ok": True}

                # Fetch URL content if available
                article_context = source_text
                if url_to_append:
                    try:
                        from app.services.search_service import SearchService
                        _ss = SearchService(db)
                        import asyncio as _asyncio
                        fetched = await _asyncio.wait_for(_ss.fetch_urls([url_to_append], max_urls=1), timeout=15)
                        if fetched and fetched[0].get("content") and not fetched[0].get("error"):
                            article_context = f"Title: {fetched[0].get('title', '')}\n\n{fetched[0]['content'][:3000]}"
                            logger.info(f"post command: fetched article, {len(article_context)} chars")
                        else:
                            logger.warning(f"post command: fetch failed or empty: {fetched[0].get('error') if fetched else 'no result'}")
                    except Exception as _fe:
                        logger.warning(f"post command: failed to fetch URL: {_fe}")

                # `arg` is free-form: a tone adjective (e.g. "professional") or an
                # explicit instruction (e.g. "don't include links", "keep it short").
                # Pass it to the model as an instruction rather than jamming it into the
                # sentence, so multi-word directions are actually honored.
                _extra = arg.strip()
                _extra_l = _extra.lower()
                # If the user asked to omit links, skip the forced URL append below too —
                # otherwise the link reappears no matter what the model does.
                _suppress_link = any(p in _extra_l for p in (
                    "no link", "no links", "without link", "don't include link",
                    "dont include link", "do not include link", "exclude link",
                    "no url", "without url", "skip link", "no source",
                )) if _extra else False
                _tone = "viral and engaging" if not _extra else "compelling"
                _user_prompt = (
                    f"Write a {_tone}, detailed social media post based on this content. "
                    f"Be detailed — include key facts, context, and why it matters. Use emojis."
                )
                if _extra:
                    _user_prompt += f"\n\nFollow these user instructions exactly: {_extra}"
                _user_prompt += f"\n\nContent:\n{article_context}"
                post_messages = [
                    {
                        "role": "system",
                        "content": "You are a social media expert. Write compelling, detailed social media posts. Output ONLY the post text. No introductions, no 'here is your post', no URL placeholders like 'link' or 'read more'."
                    },
                    {
                        "role": "user",
                        "content": _user_prompt,
                    }
                ]

                from app.services.chat_service import ChatService as _CS
                _cs = _CS(db, user=None)
                _cs.num_predict = min(_cs.num_predict, 900)
                try:
                    post_text = await _cs.chat(post_messages)
                    post_text = _strip_hashtags(post_text)
                    # Append the real URL at the end (the model may mangle it), unless
                    # the user explicitly asked to omit links.
                    if url_to_append and not _suppress_link:
                        post_text = post_text.rstrip() + f"\n\n{url_to_append}"
                    result_content = post_text
                except Exception as e:
                    result_content = f"Error generating post: {str(e)}"

                # Check if the linked user has any social platform configured
                _tg_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                await _offer_social_post(chat_id, result_content, _tg_user, telegram_service)
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
            commands = ["help", "new", "ytdl", "geni", "mail", "news", "search", "images", "yt", "torrents", "nyaa", "4chan", "logs", "translate", "post", "share", "compress", "clip", "convert", "flashcards", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "alive", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "robocop", "titan", "terminator", "reze", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz", "node", "budget", "finance", "bills", "pay", "addbill", "screenshot", "shot", "ss"]
            for cmd in commands:
                if text_lower.startswith(cmd + " ") or text_lower == cmd:
                    command = cmd
                    arg = text[len(cmd):].strip()
                    break
            # Telegram skips parse_command, so resolve aliases (e.g. shot/ss -> screenshot) to the
            # canonical name, or execute_command rejects them as "Unknown command".
            if command:
                command = CommandService.COMMAND_ALIASES.get(command, command)

            # Auto-detect bare magnet links — route to "torrents add <magnet>"
            if not command and text.strip().startswith("magnet:?"):
                command = "torrents"
                arg = f"add {text.strip()}"

            logger.warning(f"TELEGRAM: text='{text}', cmd={command}, arg='{arg}', photos={len(photos) if photos else 0}")
            
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
            
            # Now if translate command with images, do OCR
            if command == "translate" and has_images and attachments:
                # Run OCR on the image
                for filename, file_data, content_type in attachments:
                    if content_type.startswith("image/"):
                        import base64
                        image_b64 = base64.b64encode(file_data).decode('utf-8')
                        try:
                            from app.services.document_service import extract_image_text
                            ocr_result = extract_image_text(image_b64)
                            if ocr_result:
                                ocr_text = ocr_result
                                logger.warning(f"TRANSLATE: Extracted OCR text: {len(ocr_text)} chars")
                        except Exception as e:
                            logger.error(f"OCR error: {e}")
                        break
                
                if ocr_text:
                    language = arg.replace("to", "").strip() or "Thai"
                    logger.warning(f"TRANSLATE: Translating OCR text to {language}, text: {ocr_text[:50]}...")
                    
                    # Create a fresh chat service WITHOUT user context for translation
                    from app.services.chat_service import ChatService as FreshChatService
                    fresh_chat_service = FreshChatService(db, user=None)
                    
                    translate_messages = [
                        {"role": "system", "content": f"Translate the following text to {language}. Output ONLY the translation, nothing else. Do NOT add any commentary, emojis, or persona."},
                        {"role": "user", "content": ocr_text}
                    ]
                    
                    try:
                        translated = await fresh_chat_service.chat(translate_messages)
                        logger.warning(f"TRANSLATE: Got translation: {translated[:100]}...")
                        result = {"type": "text", "content": f"## Translation to {language}\n\n{translated}"}
                    except Exception as e:
                        logger.error(f"Translation error: {e}")
                        result = {"type": "text", "content": f"Translation failed: {str(e)}"}
                    
                    await telegram_service.send_message(chat_id, result.get("content", ""))
                    logger.warning(f"TRANSLATE: Sent translation result")
                    return {"ok": True}
            
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
                logger.info(f"[MEDIA-GROUP] {media_group_id}: assembled {len(attachments)} attachments, cmd={command}, text={text!r}")

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

            # If user asks to merge/combine/join multiple PDFs, do it server-side and send back as file
            # This is completely independent of PDF analysis/summarization
            _is_merge_intent = bool(re.search(r'\b(merge|combine|join|concatenate|concat)\b', text_lower)) and len(pdf_attachments) >= 2
            if _is_merge_intent:
                try:
                    _merged_bytes = merge_pdfs([_fdata for _, _fdata in pdf_attachments])
                    if _merged_bytes:
                        _names = "+".join(fn.replace('.pdf', '') for fn, _ in pdf_attachments[:3])
                        _out_name = f"merged_{_names}.pdf"
                        await telegram_service.send_document_bytes(chat_id, _merged_bytes, _out_name, f"✅ Merged {len(pdf_attachments)} PDFs into {_out_name}")
                        return {"ok": True}
                    else:
                        await telegram_service.send_message(chat_id, "❌ PDF merge failed — could not process the files.")
                        return {"ok": True}
                except Exception as _merge_err:
                    logger.error(f"PDF merge error: {_merge_err}")
                    await telegram_service.send_message(chat_id, f"❌ PDF merge failed: {_merge_err}")
                    return {"ok": True}

            # If we have images, always run OCR for later use
            # (skip for compress/convert — they operate on the raw file, not its text)
            if has_images and attachments and command not in ("compress", "clip", "convert", "flashcards", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "alive", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "robocop", "titan", "terminator", "reze", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz"):
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
            
            # If translate command with OCR text, handle it directly
            if command == "translate" and ocr_text:
                language = arg.replace("to", "").strip() or "Thai"
                logger.warning(f"TRANSLATE: Final check - Using OCR text ({len(ocr_text)} chars) to translate to '{language}'")
                logger.warning(f"TRANSLATE: ocr_text content: {ocr_text[:100]}...")
                
                # Build messages for translation
                translate_messages = [
                    {"role": "system", "content": f"Translate the following text to {language}. Output ONLY the translation, nothing else."},
                    {"role": "user", "content": ocr_text}
                ]
                
                logger.warning(f"TRANSLATE: Calling chat_service.chat with messages: {translate_messages}")
                
                try:
                    translated = await chat_service.chat(translate_messages)
                    logger.warning(f"TRANSLATE: Got translation result: {translated[:100]}...")
                    result = {"type": "text", "content": f"## Translation to {language}\n\n{translated}"}
                except Exception as e:
                    logger.error(f"Translation error: {e}", exc_info=True)
                    result = {"type": "text", "content": f"Translation failed: {str(e)}"}
                
                # Send result and return early
                await telegram_service.send_message(chat_id, result.get("content", ""))
                logger.warning(f"TRANSLATE: Sent translation result")
                return {"ok": True}
            elif command == "translate" and has_images:
                logger.warning(f"TRANSLATE: Command detected but no OCR text yet, has_images={has_images}, attachments={len(attachments)}")
            
            # Attachment too large for Telegram to hand to the bot (20 MB cap).
            # Handle here so it works whether or not a command caption was given,
            # instead of falling through to the chat model.
            if oversized_attachment and command in ("compress", "clip", "convert", "flashcards", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "alive", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "robocop", "titan", "terminator", "reze", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz", None):
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
                        f"in Admin → Services."
                    )
                await telegram_service.send_message(chat_id, _msg)
                return {"ok": True}

            # A *caption-less* media upload: prompt with action buttons (like the
            # YouTube/link prompts) instead of guessing. A caption WITH text flows
            # to normal command/chat routing so attachments never hijack features.
            if attachments and not command and not text.strip():
                _media_kbd = _media_action_keyboard(attachments, user=user_obj)
                if _media_kbd:
                    # Evict expired entries so abandoned uploads don't linger in
                    # memory (each can hold video-sized bytes).
                    _now = time.time()
                    for _cid in [k for k, v in _media_action_cache.items()
                                 if _now - v.get("ts", 0) > _MEDIA_ACTION_TTL]:
                        _media_action_cache.pop(_cid, None)
                    _media_action_cache[chat_id] = {"attachments": attachments, "ts": _now}
                    _n = len(attachments)
                    await telegram_service.send_message(
                        chat_id,
                        f"📎 Got {_n} file{'s' if _n != 1 else ''}. What would you like to do?",
                        reply_markup=_media_kbd,
                    )
                    return {"ok": True}

            reply_markup = None
            if command:
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
                            db.query(Message).filter(Message.conversation_id == tg_conv.id).delete()
                            db.commit()
                        await telegram_service.send_message(chat_id, "Conversation cleared. Starting fresh!")
                        return {"ok": True}
                    elif command in ("budget", "finance"):
                        await _send_budget(chat_id, user_obj, db)
                        return {"ok": True}
                    elif command in ("bills", "pay", "addbill"):
                        # pay/addbill mutate, then show the refreshed interactive budget;
                        # bills just returns the formatted list from the shared command service.
                        result = await command_service.execute_command(command, arg)
                        await telegram_service.send_message(
                            chat_id, result.get("content", "Done."), parse_mode=""
                        )
                        if command in ("pay", "addbill") and arg.strip():
                            await _send_budget(chat_id, user_obj, db)
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

                        from app.models import Setting as _Setting
                        _cookies_s = db.query(_Setting).filter(_Setting.key == "ytdl_cookies_path").first()
                        _cookies_path = str(_cookies_s.value).strip() if _cookies_s and _cookies_s.value else None
                        if _cookies_path and not _os.path.isfile(_cookies_path):
                            _cookies_path = None
                        _ssl_s = db.query(_Setting).filter(_Setting.key == "ytdl_no_ssl_verify").first()
                        _no_ssl = (
                            str(_ssl_s.value).strip().lower() in ("true", "1", "yes")
                            if _ssl_s and _ssl_s.value else False
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

                        has_social = _has_misskey(user_obj) or _has_pleroma(user_obj) or _has_matrix(user_obj)

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

                        # Collect image bytes so Matrix can send the actual image
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
            else:
                # Regular chat - check for images and do OCR or pass to vision model
                from app.services.intent_service import IntentService
                intent_service = IntentService(db, user=user_obj)
                text_stripped = text.strip()

                # Detect YouTube URLs anywhere in the message
                _yt_domains = ('youtube.com/watch', 'youtu.be/', 'youtube.com/shorts/')
                _all_urls_in_text = [u for u in __import__('re').findall(r'https?://\S+', text_stripped)]
                youtube_url = next((u for u in _all_urls_in_text if any(d in u for d in _yt_domains)), None)

                # Detect an X/Twitter/Nitter status URL (downloadable via yt-dlp, no transcript so no
                # Summary option). extract_download_urls returns the x.com-normalized form (nitter
                # rewritten); keep the ORIGINAL url too so the bare/forwarded check works on the text.
                _x_orig = _x_dl = None
                if not youtube_url:
                    from app.services.youtube_service import extract_download_urls as _edl
                    for _u in _all_urls_in_text:
                        _got = _edl(_u)
                        if _got:
                            _x_orig, _x_dl = _u, _got[0]
                            break

                # YouTube URL (bare or forwarded): ask the user what they want to do
                if youtube_url and (is_forwarded or not text_stripped.replace(youtube_url, '').strip()):
                    logger.info(f"Telegram: YouTube URL detected, prompting action: {youtube_url}")
                    _youtube_action_cache[chat_id] = youtube_url
                    
                    # Check if user has social platforms configured
                    _yt_user_for_social = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()

                    # Build keyboard with social post option if any platform is configured
                    yt_keyboard = [
                        [
                            {"text": "📋 Summary",  "callback_data": "yt:summary"},
                            {"text": "🎵 MP3",      "callback_data": "yt:mp3"},
                            {"text": "🎬 Movie",    "callback_data": "yt:video"},
                        ]
                    ]
                    if _has_misskey(_yt_user_for_social) or _has_pleroma(_yt_user_for_social) or _has_matrix(_yt_user_for_social):
                        yt_keyboard.append([
                            {"text": "📣 Post", "callback_data": "yt:post"}
                        ])
                    
                    await telegram_service.send_message(
                        chat_id,
                        "🎬 What would you like to do with this video?",
                        reply_markup={"inline_keyboard": yt_keyboard},
                    )
                    return {"ok": True}

                # X/Twitter/Nitter status URL (bare or forwarded): same prompt as YouTube minus
                # Summary (tweets have no transcript). Reuses the yt: callbacks — the cached URL is
                # the x.com-normalized form, so MP3/Video/Post all download via yt-dlp's Twitter path.
                if _x_dl and (is_forwarded or not text_stripped.replace(_x_orig, '').strip()):
                    logger.info(f"Telegram: X/Nitter URL detected, prompting action: {_x_dl}")
                    _youtube_action_cache[chat_id] = _x_dl

                    _x_user_for_social = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()

                    x_keyboard = [
                        [
                            {"text": "🎵 MP3",   "callback_data": "yt:mp3"},
                            {"text": "🎬 Video", "callback_data": "yt:video"},
                        ]
                    ]
                    if _has_misskey(_x_user_for_social) or _has_pleroma(_x_user_for_social) or _has_matrix(_x_user_for_social):
                        x_keyboard.append([
                            {"text": "📣 Post", "callback_data": "yt:post"}
                        ])

                    await telegram_service.send_message(
                        chat_id,
                        "🐦 What would you like to do with this post?",
                        reply_markup={"inline_keyboard": x_keyboard},
                    )
                    return {"ok": True}

                # Forwarded messages with URLs prompt the user what to do (same as bare URL)
                if is_forwarded:
                    import re as _fwd_re
                    _fwd_url = None

                    # 1. Check entities for text_link (Miniflux puts the real article URL here)
                    for _ent in message.get("entities", []) or []:
                        if _ent.get("type") == "text_link":
                            _u = _ent.get("url", "")
                            if _u.startswith("http"):
                                _fwd_url = _u.rstrip(".,)>")
                                break

                    # 2. Check link_preview_options.url
                    if not _fwd_url:
                        _lpo = message.get("link_preview_options") or {}
                        _u = _lpo.get("url", "")
                        if _u.startswith("http"):
                            _fwd_url = _u.rstrip(".,)>")

                    # 3. Fall back to raw https:// URLs in the text
                    if not _fwd_url:
                        _fwd_raw = _fwd_re.findall(r'https?://\S+', text_stripped)
                        if _fwd_raw:
                            _fwd_url = _fwd_raw[0].rstrip(".,)>")

                    if _fwd_url:
                        logger.info(f"Telegram: Forwarded message with URL, prompting action: {_fwd_url}")
                        _link_action_cache[chat_id] = _fwd_url
                        await telegram_service.send_message(
                            chat_id,
                            f"🔗 What would you like to do with this link?\n{_fwd_url}",
                            reply_markup={
                                "inline_keyboard": [
                                    [
                                        {"text": "📋 Summary",    "callback_data": "lnk:summary"},
                                        {"text": "📸 Screenshot", "callback_data": "lnk:screenshot"},
                                    ],
                                    [
                                        {"text": "📣 Post",   "callback_data": "lnk:post"},
                                        {"text": "❌ Cancel", "callback_data": "lnk:cancel"},
                                    ],
                                ]
                            },
                        )
                        return {"ok": True}

                # Skip intent detection for bare URLs — they are never commands and the
                # LLM always fails or returns garbage for URL-only input.
                is_bare_url = (
                    text_stripped.startswith(("http://", "https://")) and
                    " " not in text_stripped
                )
                intent = None if (is_bare_url or is_forwarded) else await intent_service.detect_intent(text)
                # intent["command"] is the full command string (e.g. "geni a sunset")
                # parse it to split command name from arguments
                intent_command_str = intent.get("command", "") if intent else ""
                command, arg = command_service.parse_command(intent_command_str) if intent_command_str else (None, "")

                if command:
                    logger.info(f"Detected intent: command={command}, arg={arg}")
                    if attachments:
                        result = await command_service.execute_command(command, arg, attachments=attachments)
                    else:
                        result = await command_service.execute_command(command, arg)
                else:
                    # Regular chat - use the chat service
                    from app.models import Conversation, Message

                    # Forwarded messages and bare URLs use a clean summarization context —
                    # no history, focused system prompt to avoid hallucination loops.
                    if is_bare_url or is_forwarded:
                        messages = [
                            {"role": "system", "content": "You are a concise summarizer. Summarize the provided content clearly and in detail. Include key facts, main points, and any important details. Output only the summary, nothing else."},
                        ]
                        last_role = "system"
                    else:
                        # Build messages for the LLM - no DB conversation needed for Telegram
                        # History is managed within the Telegram chat itself
                        _system_prompt = chat_service.system_prompt.replace(
                            "{{CURRENT_DATE}}", datetime.utcnow().strftime("%Y-%m-%d")
                        )
                        # Casual Telegram chat: keep it conversational and stop the
                        # model from drifting into code/script dumps for short or
                        # ambiguous messages (a recurring failure mode).
                        _system_prompt += (
                            "\n\nThis is a casual Telegram chat. Reply conversationally and briefly. "
                            "Do NOT output code, shell scripts, or ``` code blocks unless the user's "
                            "most recent message explicitly asks you to write code."
                        )
                        messages = [
                            {"role": "system", "content": _system_prompt},
                        ]

                        last_role = "system"

                        # Add recent message history from the Telegram conversation (limited, truncated)
                        conversation = db.query(Conversation).filter(
                            Conversation.user_id == user_obj.id,
                            Conversation.title == "📱 Telegram"
                        ).order_by(Conversation.updated_at.desc()).first()

                        if conversation:
                            recent_messages = db.query(Message).filter(
                                Message.conversation_id == conversation.id
                            ).order_by(Message.id.desc()).limit(6).all()

                            HISTORY_CHAR_LIMIT = 2000  # large enough to hold a full URL summary
                            for msg in reversed(recent_messages):
                                if msg.role == last_role:
                                    continue
                                # Don't feed prior code-block replies back as context — they make
                                # the model keep emitting code (self-perpetuating loop). Skip them.
                                if msg.role == "assistant" and "```" in (msg.content or ""):
                                    continue
                                content = msg.content[:HISTORY_CHAR_LIMIT] if len(msg.content) > HISTORY_CHAR_LIMIT else msg.content
                                messages.append({"role": msg.role, "content": content})
                                last_role = msg.role
                    
                    # If there are image attachments, add them to the message for vision models
                    if has_images and attachments:
                        # Build vision-capable message content
                        vision_content = []
                        for filename, file_data, content_type in attachments:
                            if content_type.startswith("image/"):
                                import base64
                                image_b64 = base64.b64encode(file_data).decode('utf-8')
                                # Try OCR first
                                try:
                                    from app.services.document_service import extract_image_text
                                    ocr_text = extract_image_text(image_b64)
                                    if ocr_text:
                                        vision_content.append({"type": "text", "text": f"[Image OCR text:\n{ocr_text}]"})
                                        logger.info(f"Extracted OCR text, length: {len(ocr_text)}")
                                    else:
                                        # No OCR - pass image directly for vision models
                                        vision_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
                                        logger.info("No OCR text, passing image to vision model")
                                except Exception as ocr_err:
                                    logger.error(f"OCR error: {ocr_err}")
                                    # Pass image directly for vision models
                                    vision_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
                                break
                        
                        if vision_content:
                            # If no user text, add an explicit instruction so the model summarizes
                            # rather than echoing the OCR content back.
                            user_instruction = text if text.strip() else (
                                "Summarize the content in this image in detail." if is_forwarded
                                else "What does this image show?"
                            )
                            vision_content.append({"type": "text", "text": user_instruction})
                            # If last_role is user, merge with last message instead of creating duplicate
                            if last_role == "user":
                                if isinstance(messages[-1]["content"], list):
                                    messages[-1]["content"].extend(vision_content)
                                else:
                                    messages[-1]["content"] += "\n\n" + str(vision_content)
                            else:
                                messages.append({"role": "user", "content": vision_content})
                            logger.info(f"Sending vision message with {len(vision_content)} content parts")
                        else:
                            # If last_role is user, merge with last message instead of creating duplicate
                            if last_role == "user":
                                messages[-1]["content"] += "\n\n" + text
                            else:
                                messages.append({"role": "user", "content": text})
                    else:
                        # Build user message, prepending any extracted document text
                        _user_msg_text = text
                        if doc_text:
                            if text.strip():
                                _user_msg_text = f"Here is a document the user shared:\n\n{doc_text}\n\nUser's message: {text}"
                            else:
                                _user_msg_text = f"The user uploaded a document. Please summarize and explain its contents:\n\n{doc_text}"
                        # If last_role is user, merge with last message instead of creating duplicate
                        if last_role == "user":
                            messages[-1]["content"] += "\n\n" + _user_msg_text
                        else:
                            messages.append({"role": "user", "content": _user_msg_text})

                    # If the user replied to a message, inject that context so the model
                    # knows what content/URL to reference (e.g. "make a post with this URL").
                    if reply_text:
                        reply_prefix = f"[Replying to: {reply_text}]\n\n"
                        if isinstance(messages[-1]["content"], list):
                            messages[-1]["content"].append({"type": "text", "text": reply_prefix})
                        else:
                            messages[-1]["content"] = reply_prefix + messages[-1]["content"]

                    # Detect and fetch URLs in user message and reply context (like web UI does)
                    from app.services.search_service import SearchService
                    search_service = SearchService(db)
                    url_context = ""
                    urls = SearchService.extract_urls(text + " " + reply_text)

                    # Deduplicate URLs: www.example.com and example.com are the same article.
                    # Normalize by stripping scheme + www prefix for comparison.
                    if urls:
                        def _url_key(u: str) -> str:
                            import re as _re
                            return _re.sub(r'^https?://(www\.)?', '', u.lower().rstrip('/'))
                        seen_keys: set = set()
                        deduped: list = []
                        for u in urls:
                            k = _url_key(u)
                            if k not in seen_keys:
                                seen_keys.add(k)
                                deduped.append(u)
                        if len(deduped) < len(urls):
                            logger.info(f"Telegram: Deduplicated URLs {urls} -> {deduped}")
                        urls = deduped

                    # Check if message is ONLY a URL (no other text)
                    is_only_url = False
                    if urls and len(text.strip()) < 500:
                        text_without_urls = text
                        for url in urls:
                            text_without_urls = text_without_urls.replace(url, '').strip()
                            if url.startswith("https://"):
                                text_without_urls = text_without_urls.replace(url[len("https://"):], '').strip()
                        is_only_url = not text_without_urls

                    # If message is only a URL, ask what the user wants to do with it.
                    # Embed the URL in the message text so the lnk: callback can recover
                    # it from the message if the in-memory cache is lost (e.g. server restart).
                    if is_only_url and urls:
                        _link_action_cache[chat_id] = urls[0]
                        await telegram_service.send_message(
                            chat_id,
                            f"🔗 What would you like to do with this link?\n{urls[0]}",
                            reply_markup={
                                "inline_keyboard": [
                                    [
                                        {"text": "📋 Summary",    "callback_data": "lnk:summary"},
                                        {"text": "📸 Screenshot", "callback_data": "lnk:screenshot"},
                                    ],
                                    [
                                        {"text": "📣 Post",   "callback_data": "lnk:post"},
                                        {"text": "❌ Cancel", "callback_data": "lnk:cancel"},
                                    ],
                                ]
                            },
                        )
                        return {"ok": True}

                    if urls:
                        logger.info(f"Telegram: Detected URLs in message: {urls}")
                        MAX_URL_CONTENT_CHARS = 2000  # Truncation only — no content cleaning
                        try:
                            fetched = await asyncio.wait_for(
                                search_service.fetch_urls(urls, max_urls=3),
                                timeout=15
                            )
                            for result in fetched:
                                if result.get("content") and not result.get("error"):
                                    content = result['content']
                                    if len(content) > MAX_URL_CONTENT_CHARS:
                                        content = content[:MAX_URL_CONTENT_CHARS] + "\n...[content truncated]"
                                    logger.info(f"Telegram: Fetched {len(result['content'])} chars (using {len(content)}) from {result['url']}")
                                    url_context += f"\n\n---\nContent from {result['url']}:\nTitle: {result['title']}\n\n{content}\n---"
                                elif result.get("error"):
                                    logger.warning(f"Telegram: Failed to fetch {result['url']}: {result['error']}")
                                    url_context += f"\n\n[Failed to fetch {result['url']}: {result['error']}]"
                        except asyncio.TimeoutError:
                            logger.warning(f"Telegram: URL fetching timed out for: {urls}")
                            url_context = "\n\n[Note: Could not fetch URL content due to timeout]"
                    
                    # Append URL context to user message if URLs were found
                    if url_context:
                        injected = url_context

                        if injected:
                            if isinstance(messages[-1]["content"], list):
                                messages[-1]["content"].append({"type": "text", "text": injected})
                            else:
                                messages[-1]["content"] += injected
                            logger.info(f"Telegram: Added URL context ({len(url_context)} chars) to message")
                    
                    if len(messages) > 1:
                        user_content = messages[1]['content']
                        logger.info(f"Final messages structure: system={messages[0]['content'][:50]}..., user content type={type(user_content)}")
                        if isinstance(user_content, list):
                            logger.info(f"User content has {len(user_content)} parts")

                    # FINAL VALIDATION: Ensure messages alternate properly
                    validated_messages = [messages[0]]  # Keep system message
                    for msg in messages[1:]:
                        if msg['role'] != validated_messages[-1]['role']:
                            validated_messages.append(msg)
                        else:
                            # Merge with previous same-role message; handle list content gracefully
                            prev = validated_messages[-1]
                            prev_content = prev['content']
                            msg_content = msg['content']
                            if isinstance(prev_content, list) or isinstance(msg_content, list):
                                # Convert both sides to string for merging
                                prev_str = str(prev_content) if isinstance(prev_content, list) else prev_content
                                msg_str = str(msg_content) if isinstance(msg_content, list) else msg_content
                                prev['content'] = prev_str + f"\n\n{msg_str}"
                            else:
                                prev['content'] += f"\n\n{msg_content}"
                    messages = validated_messages
                    logger.info(f"Validated message sequence: {[m['role'] for m in messages]}")
                    
                    # Log messages for debugging
                    for i, m in enumerate(messages):
                        content_preview = str(m.get('content', ''))[:50] if not isinstance(m.get('content'), list) else '[vision content]'
                        logger.info(f"  Message {i}: role={m.get('role')}, content={content_preview}...")
                    
                    try:
                        result = {"type": "text", "content": await chat_service.chat(messages)}
                    except Exception as chat_err:
                        error_msg = str(chat_err)
                        logger.error(f"Telegram chat error: {error_msg}", exc_info=True)
                        if "Conversation roles must alternate" in error_msg:
                            logger.error(f"ROLE ERROR - Messages that caused error:")
                            for i, m in enumerate(messages):
                                content_preview = str(m.get('content', ''))[:100] if not isinstance(m.get('content'), list) else '[vision content]'
                                logger.error(f"  Message {i}: role={m.get('role')}, content={content_preview}...")
                        result = {"type": "text", "content": f"Sorry, I encountered an error: {error_msg}"}

                    # Save user message + bot response to the Telegram conversation so
                    # follow-up messages ("turn that into a post", "translate it", etc.)
                    # have the context they need.
                    try:
                        tg_conv = db.query(Conversation).filter(
                            Conversation.user_id == user_obj.id,
                            Conversation.title == "📱 Telegram"
                        ).order_by(Conversation.updated_at.desc()).first()
                        if not tg_conv:
                            tg_conv = Conversation(user_id=user_obj.id, title="📱 Telegram")
                            db.add(tg_conv)
                            db.flush()
                        # Save the raw user text (not the injected URL content — keep history short).
                        # Save the full bot reply so follow-ups ("turn that into a post") have
                        # complete context — truncating to 500 chars cut off summaries mid-sentence.
                        db.add(Message(conversation_id=tg_conv.id, role="user", content=text))
                        bot_reply = result.get("content", "")
                        APOLOGY = "I apologize, I wasn't able to generate a proper response. Please try again."
                        # Don't save errors, apologies, or truncated responses (they corrupt future context)
                        _reply_looks_complete = bot_reply and not (len(bot_reply) < 80 and bot_reply.rstrip().endswith(":"))
                        if _reply_looks_complete and bot_reply != APOLOGY and not bot_reply.startswith("Error:") and not bot_reply.startswith("Sorry,"):
                            db.add(Message(conversation_id=tg_conv.id, role="assistant", content=bot_reply))
                        tg_conv.updated_at = datetime.utcnow()
                        db.commit()
                    except Exception as _save_err:
                        logger.warning(f"Failed to save Telegram history: {_save_err}")
                        try:
                            db.rollback()
                        except Exception:
                            pass
            
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
                else:
                    # Offer to share the generated image to configured social platforms
                    _geni_user = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()
                    if _geni_user and (_has_misskey(_geni_user) or _has_pleroma(_geni_user) or _has_matrix(_geni_user)):
                        _geni_caption = response_content or "Generated image"
                        # Store image BYTES so Matrix/Misskey/Pleroma share paths (which all
                        # pass this as image_bytes to send_image) get raw bytes — matching the
                        # pasted-image path. image_data from generate_image is base64 (optionally
                        # a data: URL); storing the base64 string broke image posts on all
                        # platforms (Matrix most visibly).
                        _geni_bytes = image_data
                        if isinstance(_geni_bytes, str):
                            import base64 as _geni_b64
                            if _geni_bytes.startswith("data:image"):
                                _geni_bytes = _geni_bytes.split(",", 1)[1]
                            try:
                                _geni_bytes = _geni_b64.b64decode(_geni_bytes)
                            except Exception:
                                _geni_bytes = None
                        # Store caption in platform caches using the same offer-post format so
                        # message-text recovery strips the suffix correctly on restart
                        _misskey_post_cache[chat_id] = _geni_caption
                        _pleroma_post_cache[chat_id] = _geni_caption
                        _matrix_post_cache[chat_id] = _geni_caption
                        await _offer_social_post(
                            chat_id, _geni_caption, _geni_user, telegram_service,
                            prompt="📣 *Share this image?*", image_bytes=_geni_bytes
                        )
            elif response_type == "flashcards":
                # Interactive multiple-choice study quiz — store the deck per chat and send card 0
                # as a PNG with answer buttons; fc: callbacks navigate/reveal in place.
                _fc_cards = result.get("cards") or []
                if not _fc_cards:
                    await telegram_service.send_message(chat_id, response_content or "Couldn't make flashcards.")
                else:
                    if result.get("note"):
                        await telegram_service.send_message(chat_id, result["note"])
                    _deck = {
                        "title": result.get("title") or "Flashcards",
                        "cards": _fc_cards,
                        "idx": 0,
                        "answered": [None] * len(_fc_cards),
                        "score": 0,
                        "ts": time.time(),
                    }
                    _flashcard_decks_cache[chat_id] = _deck
                    await _send_flashcard(chat_id, _deck)
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
                # After an effect, offer to post the result to the user's timeline.
                if command in CommandService.MOTION_EFFECTS:
                    _share = next(
                        (f for f in files if f.get("data") and (f.get("content_type") or "").startswith(("image/", "video/"))),
                        None,
                    )
                    _share_u = db.query(User).filter(
                        User.telegram_chat_id == chat_id, User.telegram_enabled == True
                    ).first() if _share else None
                    if _share and _share_u and (_has_misskey(_share_u) or _has_pleroma(_share_u) or _has_matrix(_share_u)):
                        _media_action_cache[chat_id] = {
                            "attachments": [(_share.get("filename", "file"), _share["data"], _share.get("content_type", ""))],
                            "ts": time.time(),
                        }
                        await telegram_service.send_message(
                            chat_id, "📣 Post this to your timeline?",
                            reply_markup={"inline_keyboard": [[
                                {"text": "📣 Post to social", "callback_data": "media:post"},
                            ]]},
                        )
            else:
                await telegram_service.send_message(chat_id, response_content, reply_markup=reply_markup)

            return {"ok": True}
        
        callback_query = update.get("callback_query")
        if callback_query:
            # Handle inline button callbacks
            chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id"))
            data = callback_query.get("data", "")
            callback_query_id = callback_query.get("id")

            logger.info(f"Received Telegram callback query: {data}")

            # Acknowledge immediately so Telegram removes the loading spinner
            await telegram_service.answer_callback_query(callback_query_id)

            if data.startswith("t:"):
                # Torrent inline button — look up the linked user and run the command
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

            elif data == "ytdlv:send":
                # "Send as-is" after `ytdl video` — deliver the cached download as a video.
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

            elif data == "media:fc":
                # 🎴 Flashcards button on an uploaded file → build the quiz from the cached upload.
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the file again.")
                    return {"ok": True}
                await telegram_service.send_message(chat_id, "🎴 Generating flashcards…")
                _res = await CommandService(db, user=cb_user).execute_command(
                    "flashcards", "", attachments=_entry["attachments"])
                if _res.get("type") == "flashcards" and _res.get("cards"):
                    if _res.get("note"):
                        await telegram_service.send_message(chat_id, _res["note"])
                    _deck = {"title": _res.get("title") or "Flashcards", "cards": _res["cards"],
                             "idx": 0, "answered": [None] * len(_res["cards"]), "score": 0, "ts": time.time()}
                    _flashcard_decks_cache[chat_id] = _deck
                    await _send_flashcard(chat_id, _deck)
                else:
                    await telegram_service.send_message(chat_id, _res.get("content") or "Couldn't make flashcards from that file.")
                return {"ok": True}

            elif data.startswith("fc:"):
                # Flashcard quiz navigation/answer. State lives in _flashcard_decks_cache[chat_id].
                _deck = _flashcard_decks_cache.get(chat_id)
                _mid = (callback_query.get("message") or {}).get("message_id")
                if not _deck or (time.time() - _deck.get("ts", 0)) > _FLASHCARD_TTL:
                    _flashcard_decks_cache.pop(chat_id, None)
                    await telegram_service.answer_callback_query(
                        callback_query_id, "This quiz expired — send the file again.", show_alert=True)
                    return {"ok": True}
                _deck["ts"] = time.time()
                _parts = data.split(":")
                _act = _parts[1] if len(_parts) > 1 else ""
                _total = len(_deck["cards"])
                if _act == "ans":
                    _idx = _deck["idx"]
                    try:
                        _opt = int(_parts[2])
                    except (IndexError, ValueError):
                        _opt = -1
                    if _deck["answered"][_idx] is None and 0 <= _opt < len(_deck["cards"][_idx].get("options", [])):
                        _deck["answered"][_idx] = _opt
                        if _opt == _deck["cards"][_idx].get("correct"):
                            _deck["score"] += 1
                        await _send_flashcard(chat_id, _deck, message_id=_mid)
                elif _act == "next" and _deck["idx"] < _total - 1:
                    _deck["idx"] += 1
                    await _send_flashcard(chat_id, _deck, message_id=_mid)
                elif _act == "prev" and _deck["idx"] > 0:
                    _deck["idx"] -= 1
                    await _send_flashcard(chat_id, _deck, message_id=_mid)
                elif _act == "restart":
                    _deck["idx"] = 0
                    _deck["score"] = 0
                    _deck["answered"] = [None] * _total
                    await _send_flashcard(chat_id, _deck, message_id=_mid)
                return {"ok": True}

            elif data.startswith("media:"):
                # Uploaded-file action buttons (compress / convert / read text / summarize)
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}

                # Keep the entry (don't pop) so multiple actions can run on the
                # same upload; it expires by TTL or is overwritten by the next upload.
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the file again.")
                    return {"ok": True}

                _atts = _entry["attachments"]
                _action = data.split(":", 1)[1]
                from app.services.media_service import is_image, is_video, is_pdf
                cb_command_service = CommandService(db, user=cb_user)

                async def _send_files_result(result, offer_share: bool = True):
                    """Deliver a CommandService 'files' result (callback-scope wrapper
                    around the module-level `_deliver_files_result`)."""
                    await _deliver_files_result(chat_id, cb_user, result, offer_share)

                try:
                    if _action == "compress":
                        await telegram_service.send_message(chat_id, "🗜 Compressing…")
                        _cres = await cb_command_service.execute_command("compress", "", attachments=_atts)
                        await _send_files_result(_cres, offer_share=False)
                        # For a ytdl download, offer to post the compressed result.
                        if _entry.get("ytdl") and _cres.get("files") and _cres["files"][0].get("data"):
                            _cf = _cres["files"][0]
                            await _offer_ytdl_share(chat_id, _cf.get("filename", "video.mp4"), _cf["data"], db)
                    elif _action in ("clip", "clipcompress"):
                        # Kick off the interactive trim: ask for the start time. The end
                        # time is requested after the user replies (see ForceReply routing).
                        # "clipcompress" also compresses the clipped result; that intent is
                        # stashed on the cache entry so it survives the two-step prompt.
                        if not any(is_video(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to clip — that upload has no video.")
                        else:
                            _clip_pending.pop(chat_id, None)
                            _entry["compress_after"] = (_action == "clipcompress")
                            await telegram_service.send_message(
                                chat_id, _CLIP_START_PROMPT,
                                reply_markup={"force_reply": True, "selective": True,
                                              "input_field_placeholder": "0:10"},
                            )
                    elif _action == "topdf":
                        _imgs = [a for a in _atts if is_image(a[0], a[2])]
                        await _send_files_result(await cb_command_service.execute_command("convert", "pdf", attachments=_imgs), offer_share=False)
                    elif _action == "toimg":
                        _pdfs = [a for a in _atts if is_pdf(a[0], a[2])]
                        await telegram_service.send_message(chat_id, "🖼 Converting…")
                        await _send_files_result(await cb_command_service.execute_command("convert", "images", attachments=_pdfs), offer_share=False)
                    elif _action == "ocr":
                        import base64 as _ocr_b64
                        from app.services.document_service import extract_image_text
                        _texts = []
                        for _fn, _fd, _ct in _atts:
                            if is_image(_fn, _ct):
                                _t = extract_image_text(_ocr_b64.b64encode(_fd).decode())
                                if _t:
                                    _texts.append(_t)
                        await telegram_service.send_message(chat_id, ("🔤 *Extracted text:*\n\n" + "\n\n".join(_texts)) if _texts else "No text found in the image(s).")
                    elif _action == "post":
                        # Prompt for an optional caption before showing the platform
                        # buttons. The reply is routed (see _SOCIAL_CAPTION_PROMPT) back
                        # into _offer_social_post with the media pulled from the cache.
                        _media = next((fd for fn, fd, ct in _atts if is_image(fn, ct)), None) \
                            or next((fd for fn, fd, ct in _atts if is_video(fn, ct)), None)
                        if not _media:
                            await telegram_service.send_message(chat_id, "Nothing to post.")
                        else:
                            await telegram_service.send_message(
                                chat_id, _SOCIAL_CAPTION_PROMPT,
                                reply_markup={"force_reply": True, "selective": True,
                                              "input_field_placeholder": "Caption (optional)"},
                            )
                    elif _action == "summarize":
                        import base64 as _sum_b64
                        from app.services.document_service import extract_pdf_text
                        _doc = "\n\n".join(
                            extract_pdf_text(_sum_b64.b64encode(_fd).decode()) or ""
                            for _fn, _fd, _ct in _atts if is_pdf(_fn, _ct)
                        ).strip()
                        if not _doc:
                            await telegram_service.send_message(chat_id, "Couldn't extract any text from the PDF.")
                        else:
                            _summary = await cb_command_service.chat_service.chat([
                                {"role": "system", "content": "Summarize the following document concisely. Output only the summary."},
                                {"role": "user", "content": _doc[:12000]},
                            ])
                            await telegram_service.send_message(chat_id, f"📝 *Summary:*\n\n{_summary}")
                    elif _action == "effects":
                        # Open the Effects submenu (meme / dildo / poo). The image stays in
                        # the cache; the submenu's buttons reuse the existing actions.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        else:
                            await telegram_service.send_message(
                                chat_id, "✨ Effects — pick a category:",
                                reply_markup=_media_effects_keyboard(),
                            )
                    elif _action.startswith("fxcat:"):
                        # An Effects category was chosen → show that sub-keyboard.
                        _cat = _action.split(":", 1)[1]
                        _cat_kbd = {
                            "themes": _media_fx_themes_keyboard,
                            "sounds": _media_fx_sounds_keyboard,
                            "memes": _media_fx_memes_keyboard,
                        }.get(_cat)
                        if not _cat_kbd:
                            await telegram_service.send_message(chat_id, "Unknown effects category.")
                        elif not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        else:
                            _cat_label = {"themes": "📺 TV/Movie Themes", "sounds": "🔊 Sound clips", "memes": "🎨 Memes / overlays"}[_cat]
                            await telegram_service.send_message(
                                chat_id, f"{_cat_label}:", reply_markup=_cat_kbd(),
                            )
                    elif _action == "back":
                        # Return from the Effects submenu to the main file actions.
                        _kbd = _media_action_keyboard(_atts, user=cb_user)
                        if _kbd:
                            await telegram_service.send_message(
                                chat_id, "📎 File actions:", reply_markup=_kbd,
                            )
                    elif _action.startswith("zq:"):
                        # Effect chosen from the Effects menu → offer a motion (zoom
                        # pan-out / camera shake). "No motion" reuses the effect's own
                        # media:<eff> handler.
                        _eff = _action.split(":", 1)[1]
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        elif _eff in CommandService.ANIMATED_EFFECTS:
                            # Already-animated effect — zoom/shake would freeze it, so skip the motion
                            # menu, but STILL offer the caption (meme text overlays fine on the video).
                            _effect_caption_pending[chat_id] = {"eff": _eff, "motion": "", "ts": time.time()}
                            await telegram_service.send_message(
                                chat_id, "📝 Add a caption?",
                                reply_markup={"inline_keyboard": [[
                                    {"text": "✍️ Add text", "callback_data": "media:capq:add"},
                                    {"text": "▶️ No, render", "callback_data": "media:capq:skip"},
                                ]]},
                            )
                        else:
                            # Left column = motion alone; right column = the same motion
                            # with the trippy hue-cycle layered on top (the only combo
                            # that composes — geometry motions don't stack). 🌈 Trippy
                            # alone + ❌ None on the last row.
                            _rows = [
                                [
                                    {"text": "🔍 Zoom", "callback_data": f"media:mo:dz:{_eff}"},
                                    {"text": "🔍🌈 Zoom+", "callback_data": f"media:mo:dzt:{_eff}"},
                                ],
                                [
                                    {"text": "📳 Shake", "callback_data": f"media:mo:sh:{_eff}"},
                                    {"text": "📳🌈 Shake+", "callback_data": f"media:mo:sht:{_eff}"},
                                ],
                                [
                                    {"text": "〰️ Med", "callback_data": f"media:mo:ms:{_eff}"},
                                    {"text": "〰️🌈 Med+", "callback_data": f"media:mo:mst:{_eff}"},
                                ],
                                [
                                    {"text": "💥 Begin", "callback_data": f"media:mo:bs:{_eff}"},
                                    {"text": "💥🌈 Begin+", "callback_data": f"media:mo:bst:{_eff}"},
                                ],
                                [
                                    {"text": "💓 Pulse", "callback_data": f"media:mo:pl:{_eff}"},
                                    {"text": "💓🌈 Pulse+", "callback_data": f"media:mo:plt:{_eff}"},
                                ],
                                [
                                    {"text": "🪄 Alive (3D)", "callback_data": f"media:mo:al:{_eff}"},
                                    {"text": "🌟 Glow", "callback_data": f"media:mo:gl:{_eff}"},
                                ],
                                [
                                    {"text": "🌈 Trippy", "callback_data": f"media:mo:tr:{_eff}"},
                                    {"text": "❌ None", "callback_data": f"media:mo:none:{_eff}"},
                                ],
                            ]
                            await telegram_service.send_message(
                                chat_id, "✨ Add motion?", reply_markup={"inline_keyboard": _rows},
                            )
                    elif _action.startswith("mo:"):
                        # A motion (and optional trippy combo) was chosen. Code maps to
                        # the command arg, e.g. "dzt" → "zoom trippy", "none" → no motion.
                        # Caption is the FINAL step: after the motion we ask "Add text?"
                        # so any motion can be combined with a meme caption.
                        _, _code, _eff = _action.split(":", 2)
                        _motion = {
                            "dz": "zoom", "dzt": "zoom trippy",
                            "sh": "shake", "sht": "shake trippy",
                            "ms": "medshake", "mst": "medshake trippy",
                            "bs": "beginshake", "bst": "beginshake trippy",
                            "pl": "pulse", "plt": "pulse trippy",
                            "al": "alive", "gl": "glow",
                            "tr": "trippy", "none": "",
                        }.get(_code, "")
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        elif _eff == "thug":
                            # thug bakes its own "THUG LIFE" text — no custom caption; render now.
                            await telegram_service.send_message(chat_id, f"✨ {_eff}{(' + ' + _motion) if _motion else ''}…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command(_eff, _motion, attachments=_imgs))
                        else:
                            # Remember the effect + chosen motion, then offer the caption.
                            _effect_caption_pending[chat_id] = {"eff": _eff, "motion": _motion, "ts": time.time()}
                            await telegram_service.send_message(
                                chat_id, "📝 Add a caption?",
                                reply_markup={"inline_keyboard": [[
                                    {"text": "✍️ Add text", "callback_data": "media:capq:add"},
                                    {"text": "▶️ No, render", "callback_data": "media:capq:skip"},
                                ]]},
                            )
                    elif _action.startswith("capq:"):
                        # Caption decision after a motion was picked. "add" → ForceReply
                        # for the text (render happens on the reply); "skip" → render now
                        # with just the motion.
                        _decision = _action.split(":", 1)[1]
                        _pend = _effect_caption_pending.get(chat_id)
                        if not _pend or (time.time() - _pend.get("ts", 0)) > _MEDIA_ACTION_TTL:
                            await telegram_service.send_message(chat_id, "⏳ That upload expired — tap the effect again.")
                            _effect_caption_pending.pop(chat_id, None)
                        elif not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                            _effect_caption_pending.pop(chat_id, None)
                        elif _decision == "add":
                            _pend["ts"] = time.time()
                            await telegram_service.send_message(
                                chat_id, _EFFECT_CAPTION_PROMPT,
                                reply_markup={"force_reply": True, "selective": True,
                                              "input_field_placeholder": "TOP TEXT"},
                            )
                        else:  # skip caption → the character step is the FINAL one; render there.
                            _eff, _motion = _pend["eff"], _pend.get("motion", "")
                            _effect_char_pending[chat_id] = {"eff": _eff, "motion": _motion, "caption": "", "ts": time.time()}
                            _effect_caption_pending.pop(chat_id, None)
                            await telegram_service.send_message(
                                chat_id, "🧸 Add a character (bottom-right)?",
                                reply_markup=_character_prompt_keyboard(),
                            )
                    elif _action.startswith("chr:"):
                        # FINAL step: a character (or "none") was chosen. Render ONCE with a combined
                        # arg so the shared parser applies motion + character + caption together.
                        _char = _action.split(":", 1)[1]
                        _pend = _effect_char_pending.get(chat_id)
                        if not _pend or (time.time() - _pend.get("ts", 0)) > _MEDIA_ACTION_TTL:
                            _effect_char_pending.pop(chat_id, None)
                            await telegram_service.send_message(chat_id, "⏳ That upload expired — tap the effect again.")
                        elif not any(is_image(fn, ct) for fn, _, ct in _atts):
                            _effect_char_pending.pop(chat_id, None)
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        else:
                            _e = _pend["eff"]; _m = _pend.get("motion", ""); _c = _pend.get("caption", "")
                            _parts = []
                            if _m:
                                _parts.append(_m)
                            if _char != "none":
                                _parts.append(f"char {_char}")
                            if _c:
                                _parts.append(f"meme {_c}")
                            _arg = " ".join(_parts).strip()
                            _lbl = _e + (f" + {_char}" if _char != "none" else "")
                            await telegram_service.send_message(chat_id, f"✨ {_lbl}…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command(_e, _arg, attachments=_imgs))
                            _effect_char_pending.pop(chat_id, None)
                    elif _action == "meme":
                        # ForceReply for the caption; the image stays in the cache and is
                        # captioned when the reply arrives (see _MEME_PROMPT routing).
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to caption — that upload has no image.")
                        else:
                            await telegram_service.send_message(
                                chat_id, _MEME_PROMPT,
                                reply_markup={"force_reply": True, "selective": True,
                                              "input_field_placeholder": "TOP TEXT"},
                            )
                    elif _action == "dildo":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🍆 Adding dildos…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            # Send as a document (not send_photo): the result is a JPEG,
                            # whose base64 starts with "/9j/" — send_photo would treat that
                            # as a file path and fail.
                            await _send_files_result(await cb_command_service.execute_command("dildo", "", attachments=_imgs))
                    elif _action == "poo":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💩 Adding poop…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("poo", "", attachments=_imgs))
                    elif _action == "cum":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💦 Adding cum…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("cum", "", attachments=_imgs))
                    elif _action == "blood":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🩸 Adding blood…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("blood", "", attachments=_imgs))
                    elif _action == "bullethole":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🕳️ Adding bullet holes…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("bullethole", "", attachments=_imgs))
                    elif _action == "fire":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🔥 Setting it on fire…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("fire", "", attachments=_imgs))
                    elif _action == "glow":
                        # Enter the shared caption → character → render flow (so glow can get text +
                        # a character too, and the branding outro, like the other effects).
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to enhance — that upload has no image.")
                        else:
                            _effect_caption_pending[chat_id] = {"eff": "glow", "motion": "", "ts": time.time()}
                            await telegram_service.send_message(
                                chat_id, "📝 Add a caption?",
                                reply_markup={"inline_keyboard": [[
                                    {"text": "✍️ Add text", "callback_data": "media:capq:add"},
                                    {"text": "▶️ No, render", "callback_data": "media:capq:skip"},
                                ]]},
                            )
                    elif _action == "gay":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to stamp — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🏳️‍🌈 Stamping…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("gay", "", attachments=_imgs))
                    elif _action == "blacked":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to stamp — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🥷 Slapping the logo on…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("blacked", "", attachments=_imgs))
                    elif _action == "kosher":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to stamp — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "✡️ Certifying kosher…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("kosher", "", attachments=_imgs))
                    elif _action == "blue":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to paint — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🔵 Dripping blue paint…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("blue", "", attachments=_imgs))
                    elif _action == "barked":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to bark at — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🐶 Barking…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("barked", "", attachments=_imgs))
                    elif _action == "hava":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎻 Hava Nagila-ing…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("hava", "", attachments=_imgs))
                    elif _action == "indian":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🇮🇳 Adding the song…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("indian", "", attachments=_imgs))
                    elif _action == "yakety":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎷 Yakety Sax-ing…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("yakety", "", attachments=_imgs))
                    elif _action == "yamete":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🛑 Yamete kudasai…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("yamete", "", attachments=_imgs))
                    elif _action == "curb":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😬 Curbing…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("curb", "", attachments=_imgs))
                    elif _action == "depressing":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😢 Getting depressing…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("depressing", "", attachments=_imgs))
                    elif _action == "fahh":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🌀 Fahh…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("fahh", "", attachments=_imgs))
                    elif _action == "helpme":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🆘 Helpme…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("helpme", "", attachments=_imgs))
                    elif _action == "gong":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🔔 Gong…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("gong", "", attachments=_imgs))
                    elif _action == "fbi":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🚨 FBI OPEN UP…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("fbi", "", attachments=_imgs))
                    elif _action == "redeem":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💳 Do NOT redeem it…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("redeem", "", attachments=_imgs))
                    elif _action == "gigity":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😏 Gigity…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("gigity", "", attachments=_imgs))
                    elif _action == "beavis":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🤤 Beavis…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("beavis", "", attachments=_imgs))
                    elif _action == "smell":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "👃 Can you imagine the smell…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("smell", "", attachments=_imgs))
                    elif _action == "hood":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🏚️ Hood…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("hood", "", attachments=_imgs))
                    elif _action == "akbar":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🕌 Akbar…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("akbar", "", attachments=_imgs))
                    elif _action == "retard":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "⚠️ Retard alert…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("retard", "", attachments=_imgs))
                    elif _action == "whoabuddy":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🤠 Whoa buddy…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("whoabuddy", "", attachments=_imgs))
                    elif _action == "robocop":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🤖 Robocop…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("robocop", "", attachments=_imgs))
                    elif _action == "titan":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🗿 Titan…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("titan", "", attachments=_imgs))
                    elif _action == "terminator":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🦾 Terminator…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("terminator", "", attachments=_imgs))
                    elif _action == "reze":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💣 Reze…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("reze", "", attachments=_imgs))
                    elif _action == "feliz":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎉 Feliz…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("feliz", "", attachments=_imgs))
                    elif _action == "prayer":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to the prayer clip — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🙏 Prayer…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("prayer", "", attachments=_imgs))
                    elif _action == "alive":
                        # 3D parallax — enter the shared caption → character → render flow.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to animate — that upload has no image.")
                        else:
                            _effect_caption_pending[chat_id] = {"eff": "alive", "motion": "", "ts": time.time()}
                            await telegram_service.send_message(
                                chat_id, "📝 Add a caption?",
                                reply_markup={"inline_keyboard": [[
                                    {"text": "✍️ Add text", "callback_data": "media:capq:add"},
                                    {"text": "▶️ No, render", "callback_data": "media:capq:skip"},
                                ]]},
                            )
                    elif _action == "sopranos":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🇮🇹 Sopranos…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("sopranos", "", attachments=_imgs))
                    elif _action == "cheers":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🍻 Cheers…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("cheers", "", attachments=_imgs))
                    elif _action == "munsters":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🧛 Munsters…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("munsters", "", attachments=_imgs))
                    elif _action == "happydays":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🕺 Happy Days…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("happydays", "", attachments=_imgs))
                    elif _action == "dontwanttowait":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🌊 Don't Want to Wait…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("dontwanttowait", "", attachments=_imgs))
                    elif _action == "strangerthings":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🔦 Stranger Things…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("strangerthings", "", attachments=_imgs))
                    elif _action == "adamsfamily":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🖤 Addams Family…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("adamsfamily", "", attachments=_imgs))
                    elif _action == "xmen":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "❌ X-Men…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("xmen", "", attachments=_imgs))
                    elif _action == "futurama":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🚀 Futurama…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("futurama", "", attachments=_imgs))
                    elif _action == "charliesangles":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "👼 Charlie's Angels…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("charliesangles", "", attachments=_imgs))
                    elif _action == "differentstroke":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🌍 Diff'rent Strokes…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("differentstroke", "", attachments=_imgs))
                    elif _action == "seinfeld":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎤 Seinfeld…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("seinfeld", "", attachments=_imgs))
                    elif _action == "onepiece":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🏴‍☠️ One Piece…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("onepiece", "", attachments=_imgs))
                    elif _action == "overtaken":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🏎️ Overtaken…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("overtaken", "", attachments=_imgs))
                    elif _action == "freebird":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🦅 Free Bird…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("freebird", "", attachments=_imgs))
                    elif _action == "kanye":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🐻 Kanye…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("kanye", "", attachments=_imgs))
                    elif _action == "darkness":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🌑 Darkness…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("darkness", "", attachments=_imgs))
                    elif _action == "bike":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🚲 Bike…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("bike", "", attachments=_imgs))
                    elif _action == "jobs":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💼 They took our jobs…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("jobs", "", attachments=_imgs))
                    elif _action == "ree":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😡 REEEE…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("ree", "", attachments=_imgs))
                    elif _action == "liberal":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🗽 Liberal…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("liberal", "", attachments=_imgs))
                    elif _action == "moving":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "📦 Moving…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("moving", "", attachments=_imgs))
                    elif _action == "harlem":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🕺 Harlem Shake…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("harlem", "", attachments=_imgs))
                    elif _action == "chimp":
                        # No caption needed — render the overlay video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to overlay — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🐵 Chimp…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("chimp", "", attachments=_imgs))
                    elif _action == "consider":
                        # Image overlay — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🤔 Consider the following…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("consider", "", attachments=_imgs))
                    elif _action == "clay":
                        # Animated overlay — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to overlay — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🗣️ Sheeeit…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("clay", "", attachments=_imgs))
                    elif _action == "wasteland":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎸 Teenage wasteland…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("wasteland", "", attachments=_imgs))
                    elif _action == "mixalot":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🍑 Baby got back…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("mixalot", "", attachments=_imgs))
                    elif _action == "thug":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😎 THUG LIFE…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("thug", "", attachments=_imgs))
                    elif _action == "translate":
                        # Ask which language to translate the upload's text into.
                        await telegram_service.send_message(
                            chat_id, "🌐 Translate to which language?",
                            reply_markup=_media_translate_keyboard(),
                        )
                    elif _action.startswith("tr:"):
                        _lang = _action[3:].strip() or "english"
                        await telegram_service.send_message(chat_id, f"🌐 Translating to {_lang.title()}…")
                        # Shared helper: OCRs the upload and translates the FULL text
                        # (raised output budget so long pages don't get cut off).
                        _res = await cb_command_service._translate_command(_lang, attachments=_atts)
                        if _res.get("error") == "no_text":
                            # Almost always a Telegram-compressed photo (a tall screenshot
                            # gets shrunk too narrow to read) — point at the File workaround.
                            _txt = ("📸 Couldn't read any text in that image. Telegram compresses photos, "
                                    "so a tall screenshot gets shrunk too small to read.\n\n"
                                    "Send it as a *File* (📎 attach → File) instead of a photo for full "
                                    "resolution, then tap 🌐 Translate.")
                        else:
                            _txt = _res.get("content", "Translation failed.")
                        await telegram_service.send_message(chat_id, _txt)
                except Exception as _media_err:
                    logger.error(f"Media action '{_action}' failed: {_media_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Failed: {_media_err}")

            elif data.startswith("n:"):
                # Nyaa inline button
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

            elif data.startswith("4c:"):
                # 4chan inline button
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}

                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""

                # 4chan buttons carry numeric offsets / thread ids in parts[3:]. A stale or
                # tampered button with a non-numeric value would raise ValueError on int() —
                # bail gracefully instead.
                if any(p and not p.lstrip("-").isdigit() for p in parts[3:]):
                    await telegram_service.answer_callback_query(
                        callback_query_id, text="That button is no longer valid — reopen the menu.",
                        show_alert=True)
                    return {"ok": True}

                if action == "select":
                    # Show board selector
                    await telegram_service.send_message(
                        chat_id,
                        "🍀 *4chan Board Selector*\n\nChoose a board to browse:",
                        reply_markup=_4chan_initial_keyboard()
                    )
                    return {"ok": True}

                elif action == "board" and len(parts) >= 3:
                    board = parts[2]
                    offset = int(parts[3]) if len(parts) >= 4 else 0
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_catalog(chat_id, board, user_id, offset=offset)
                    return {"ok": True}

                elif action == "catalognext" and len(parts) >= 4:
                    board = parts[2]
                    offset = int(parts[3])
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_catalog(chat_id, board, user_id, offset=offset)
                    return {"ok": True}

                elif action == "catalogprev" and len(parts) >= 4:
                    board = parts[2]
                    offset = int(parts[3])
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_catalog(chat_id, board, user_id, offset=offset)
                    return {"ok": True}

                elif action == "thread" and len(parts) >= 4:
                    board = parts[2]
                    thread_id = int(parts[3])
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_thread(chat_id, board, thread_id, user_id)
                    return {"ok": True}

                elif action == "summarize" and len(parts) >= 4:
                    board = parts[2]
                    thread_id = int(parts[3])
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_thread(chat_id, board, thread_id, user_id, summarize=True)
                    return {"ok": True}

                elif action == "refreshthread" and len(parts) >= 4:
                    board = parts[2]
                    thread_id = int(parts[3])
                    offset = int(parts[4]) if len(parts) >= 5 else 0
                    user_id = cb_user.id if cb_user else 0
                    # Send loading message
                    await telegram_service.send_message(chat_id, "🔄 Refreshing thread...")
                    # Reload the thread at current offset
                    await _send_4chan_thread(chat_id, board, thread_id, user_id, offset=offset)
                    return {"ok": True}

                elif action == "nextpage" and len(parts) >= 5:
                    board = parts[2]
                    thread_id = int(parts[3])
                    offset = int(parts[4])
                    user_id = cb_user.id if cb_user else 0
                    # Load next page
                    await _send_4chan_thread(chat_id, board, thread_id, user_id, offset=offset)
                    return {"ok": True}

                elif action == "prevpage" and len(parts) >= 5:
                    board = parts[2]
                    thread_id = int(parts[3])
                    offset = int(parts[4])
                    user_id = cb_user.id if cb_user else 0
                    # Load previous page
                    await _send_4chan_thread(chat_id, board, thread_id, user_id, offset=offset)
                    return {"ok": True}

            elif data.startswith("news:"):
                # News source selection callback
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}

                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""

                if action == "menu":
                    # Show news main menu
                    await telegram_service.send_message(
                        chat_id,
                        "📰 *News Menu*\n\nChoose an option:",
                        reply_markup=_news_menu_keyboard()
                    )
                    return {"ok": True}

                elif action == "select":
                    # Fetch user's news sources and show selector
                    from app.routers.news import get_user_news_sources
                    sources = get_user_news_sources(cb_user, db)
                    if not sources:
                        await telegram_service.send_message(
                            chat_id,
                            "📰 *News Sources*\n\nNo news sources configured.\n\nAdd sources in User Settings → News Sources."
                        )
                        return {"ok": True}
                    await _send_news_source_selector(chat_id, sources)
                    return {"ok": True}

                elif action == "config_hint":
                    await telegram_service.send_message(
                        chat_id,
                        "⚙️ *Configure News Sources*\n\nTo add or manage news sources:\n1. Open the Web UI\n2. Go to User Settings\n3. Click on 'News Sources'\n\nYou can add RSS feeds or news websites there."
                    )
                    return {"ok": True}

                elif action == "all":
                    # Fetch news from all sources
                    cb_command_service = CommandService(db, user=cb_user)
                    result = await cb_command_service.execute_command("news", "")
                    content = _strip_cmd_links(result.get("content", ""))
                    
                    # Parse articles and add buttons
                    has_social = _has_misskey(cb_user) or _has_pleroma(cb_user) or _has_matrix(cb_user)

                    articles = _split_news_into_articles(content)
                    if articles:
                        # Cache (title, url) pairs for the Post callbacks
                        _news_post_cache[chat_id] = [(title, url) for (_, title, url, _) in articles]

                        # Send header (date/source summary line) if present
                        header_match = re.match(r'^(##[^\n]+)', content)
                        if header_match:
                            await telegram_service.send_message(chat_id, header_match.group(1))

                        # Send each article as its own message with buttons
                        for i, (_, title, url, msg_text) in enumerate(articles[:10], 1):
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
                    else:
                        # Fallback: no articles parsed — send raw content
                        await telegram_service.send_message(chat_id, content)
                    return {"ok": True}

                elif action == "source" and len(parts) >= 3:
                    # Fetch news from specific source
                    try:
                        source_idx = int(parts[2]) - 1  # Convert to 0-based index
                        sources = _news_source_cache.get(chat_id, [])
                        if 0 <= source_idx < len(sources):
                            source_name = sources[source_idx].get("name", "")
                            cb_command_service = CommandService(db, user=cb_user)
                            result = await cb_command_service.execute_command("news", source_name)
                            content = _strip_cmd_links(result.get("content", ""))

                            has_social = _has_misskey(cb_user) or _has_pleroma(cb_user) or _has_matrix(cb_user)

                            articles = _split_news_into_articles(content)
                            if articles:
                                # Cache (title, url) pairs for the Post callbacks
                                _news_post_cache[chat_id] = [(title, url) for (_, title, url, _) in articles]

                                # Send header (date/source summary line) if present
                                header_match = re.match(r'^(##[^\n]+)', content)
                                if header_match:
                                    await telegram_service.send_message(chat_id, header_match.group(1))

                                # Send each article as its own message with buttons
                                for i, (_, title, url, msg_text) in enumerate(articles[:10], 1):
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
                            else:
                                # Fallback: no articles parsed — send raw content
                                await telegram_service.send_message(chat_id, content)
                        else:
                            await telegram_service.send_message(chat_id, "❌ Source not found. Please try again.")
                    except (ValueError, IndexError):
                        await telegram_service.send_message(chat_id, "❌ Invalid source selection.")
                    return {"ok": True}

                elif action == "summarize" and len(parts) >= 3:
                    # Summarize a news article
                    try:
                        article_idx = int(parts[2]) - 1  # Convert to 0-based index
                        cached_articles = _news_post_cache.get(chat_id, [])
                        if 0 <= article_idx < len(cached_articles):
                            title, url = cached_articles[article_idx]
                            await telegram_service.send_message(chat_id, f"📝 Summarizing article...")
                            # Use AI to summarize
                            from app.services.chat_service import ChatService
                            chat_service = ChatService(db, user=cb_user)
                            messages = [
                                {"role": "system", "content": "Summarize the following news article in 2-3 sentences. Be concise and factual."},
                                {"role": "user", "content": f"Title: {title}\nURL: {url}\n\nPlease summarize this article."}
                            ]
                            summary = await chat_service.chat(messages)
                            await telegram_service.send_message(
                                chat_id,
                                f"📝 *Summary*\n\n*{title}*\n\n{summary}\n\n[Read full article]({url})"
                            )
                        else:
                            await telegram_service.send_message(chat_id, "❌ Article not found. Please fetch news again.")
                    except (ValueError, IndexError):
                        await telegram_service.send_message(chat_id, "❌ Invalid article selection.")
                    return {"ok": True}

                elif action == "post" and len(parts) >= 3:
                    # Generate social media post for a news article
                    try:
                        article_idx = int(parts[2]) - 1  # Convert to 0-based index
                        cached_articles = _news_post_cache.get(chat_id, [])
                        if 0 <= article_idx < len(cached_articles):
                            title, url = cached_articles[article_idx]
                            await telegram_service.send_message(chat_id, f"📣 Generating social media post...")
                            # Use AI to generate post
                            from app.services.chat_service import ChatService
                            chat_service = ChatService(db, user=cb_user)
                            messages = [
                                {"role": "system", "content": "Generate a short, engaging social media post (under 280 characters) for this news article. Use emojis but no hashtags."},
                                {"role": "user", "content": f"Title: {title}\nURL: {url}\n\nGenerate a social media post."}
                            ]
                            post_text = _strip_hashtags(await chat_service.chat(messages))
                            await _offer_social_post(chat_id, post_text, cb_user, telegram_service)
                        else:
                            await telegram_service.send_message(chat_id, "❌ Source not found. Please try again.")
                    except (ValueError, IndexError):
                        await telegram_service.send_message(chat_id, "❌ Invalid source selection.")
                    return {"ok": True}

            elif data.startswith("fin:"):
                # Finance buttons: fin:pay:<bill_id> | fin:refresh | fin:add
                # | fin:addincome | fin:bills:<unpaid|paid|all>
                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""
                message_id = (callback_query.get("message") or {}).get("message_id")
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                elif action == "add":
                    await telegram_service.send_message(
                        chat_id,
                        "💰 Add a bill — reply: name amount",
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "e.g. Rent 1200"},
                    )
                elif action == "addincome":
                    await telegram_service.send_message(
                        chat_id,
                        _FIN_INCOME_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "e.g. Paycheck 3000"},
                    )
                elif action == "bills" and len(parts) > 2:
                    await _send_bills_list(chat_id, cb_user, db, parts[2], message_id=message_id)
                elif action == "refresh":
                    await _send_budget(chat_id, cb_user, db, message_id=message_id)
                elif action == "pay" and len(parts) > 2:
                    from app.services import finance_service
                    bill = _finance_bills_cache.get(chat_id, {}).get(parts[2])
                    if not bill:
                        await telegram_service.answer_callback_query(
                            callback_query_id, text="Bill list expired — tap Refresh.", show_alert=True)
                    else:
                        try:
                            base, key = finance_service.get_config(db, cb_user)
                            res = await finance_service.pay_bill(base, key, bill["name"])
                            await telegram_service.answer_callback_query(
                                callback_query_id, text=res.get("message", "Paid."))
                        except finance_service.FinanceError as e:
                            await telegram_service.answer_callback_query(
                                callback_query_id, text=str(e), show_alert=True)
                        await _send_budget(chat_id, cb_user, db, message_id=message_id)

            elif data.startswith("prompt:"):
                action = data.split(":", 1)[1]
                _PROMPT_CONFIGS = {
                    "search":   ("🔍 What would you like to search for?", "e.g. latest AI news"),
                    "images":   ("🖼 What images would you like to search for?", "e.g. northern lights"),
                    "geni":     ("🎨 Describe the image you want to generate:", "e.g. a sunset over a cyberpunk city"),
                    "nyaa":     ("🔎 Type your anime search:", "e.g. one piece 1080p"),
                    "torrents": ("🔍 Type your torrent search:", "e.g. dark knight 1080p"),
                    "4chan":    ("🍀 Which board? (g, pol, a, or h)", "e.g. g"),
                    "screenshot": ("📸 Send the URL to screenshot:", "e.g. example.com"),
                }
                cfg = _PROMPT_CONFIGS.get(action)
                if cfg:
                    prompt_text, placeholder = cfg
                    await telegram_service.send_message(
                        chat_id,
                        prompt_text,
                        reply_markup={"force_reply": True, "selective": True, "input_field_placeholder": placeholder},
                    )

            elif data.startswith("help:"):
                section = data.split(":", 1)[1]
                section_text = _HELP_SECTIONS.get(section)
                back_button = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "help:menu"}]]}
                if section == "menu":
                    await telegram_service.send_message(
                        chat_id,
                        "🤖 *PosterChanAI Help*\n\nTap any button below to learn about a feature:",
                        parse_mode="MarkdownV2",
                        reply_markup=_help_main_keyboard(),
                    )
                elif section == "finance":
                    # Open the interactive budget directly instead of showing help text,
                    # so finance is fully button-driven from the help menu.
                    cb_user = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()
                    if cb_user:
                        await _send_budget(chat_id, cb_user, db)
                    else:
                        await telegram_service.send_message(
                            chat_id,
                            "Your Telegram account is not linked.",
                            reply_markup=back_button,
                        )
                elif section == "logs":
                    # Execute the logs command directly instead of showing help text
                    cb_user = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()
                    if cb_user:
                        cb_command_service = CommandService(db, user=cb_user)
                        try:
                            result = await cb_command_service.execute_command("logs", "")
                            await telegram_service.send_message(
                                chat_id,
                                result.get("content", "No logs available."),
                                reply_markup=back_button,
                            )
                        except Exception as logs_err:
                            logger.error(f"Logs command error: {logs_err}", exc_info=True)
                            await telegram_service.send_message(
                                chat_id,
                                f"Error fetching logs: {logs_err}",
                                reply_markup=back_button,
                            )
                    else:
                        await telegram_service.send_message(
                            chat_id,
                            "Your Telegram account is not linked.",
                            reply_markup=back_button,
                        )
                elif section_text:
                    await telegram_service.send_message(
                        chat_id,
                        section_text,
                        parse_mode="MarkdownV2",
                        reply_markup=back_button,
                    )

            elif data.startswith("lnk:"):
                action = data.split(":", 1)[1]
                cached_url = _link_action_cache.pop(chat_id, None)

                # If cache missed (e.g. after a server restart), try to recover URL from
                # the button message text (forwarded-link prompts embed the URL there).
                if cached_url is None and action != "cancel":
                    from app.services.search_service import SearchService as _SS
                    _msg_text = (callback_query.get("message") or {}).get("text", "")
                    _recovered = _SS.extract_urls(_msg_text)
                    if _recovered:
                        cached_url = _recovered[0]
                        logger.info(f"lnk:{action} - recovered URL from message text: {cached_url}")

                if action == "cancel" or cached_url is None:
                    if action != "cancel":
                        await telegram_service.send_message(chat_id, "No pending link found. Please send the URL again.")
                    return {"ok": True}

                lnk_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if action == "summary":
                    await telegram_service.send_message(chat_id, "⏳ Fetching and summarizing link, please wait...")
                    try:
                        import asyncio as _asyncio
                        title, content, err = await _link_content_for_llm(db, cached_url)
                        if content:
                            content = content[:4000]
                            lnk_chat = ChatService(db, user=lnk_user)
                            summary_msgs = [
                                {"role": "system", "content": "You are a thorough summarizer. Output only the summary, nothing else. No introductions or meta-commentary."},
                                {"role": "user", "content": f"Title: {title}\n\n{content}\n\nWrite a detailed summary of the above. Include the key points, important facts, context, and any notable details. Use bullet points where helpful."}
                            ]
                            summary = await _asyncio.wait_for(lnk_chat.chat(summary_msgs), timeout=120)
                            await telegram_service.send_message(chat_id, summary)
                        else:
                            # No real content -> do NOT let the model invent a summary.
                            await telegram_service.send_message(chat_id, f"Could not fetch content from the URL. ({err})")
                    except _asyncio.TimeoutError:
                        await telegram_service.send_message(chat_id, "Timed out fetching or summarizing the link.")
                    except Exception as lnk_err:
                        logger.error(f"Link summary error: {lnk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error: {lnk_err}")

                elif action == "post":
                    await telegram_service.send_message(chat_id, "⏳ Generating post, please wait...")
                    try:
                        import asyncio as _asyncio
                        title, content, err = await _link_content_for_llm(db, cached_url)
                        if not content:
                            # No real content (e.g. a YouTube video with no captions) -> refuse
                            # rather than letting the model invent a post from the bare URL.
                            await telegram_service.send_message(chat_id, f"Couldn't read that link to write a post. ({err})")
                            return {"ok": True}
                        article_context = f"Title: {title}\n\n{content[:3000]}"

                        post_messages = [
                            {
                                "role": "system",
                                "content": "You are a social media expert. Write compelling, detailed social media posts. Output ONLY the post text. No introductions, no 'here is your post', no URL placeholders like 'link' or 'read more'."
                            },
                            {
                                "role": "user",
                                "content": (
                                    "Write a viral and engaging social media post based on this content. "
                                    "Be detailed — include key facts, context, and why it matters. "
                                    "Use emojis.\n\n"
                                    f"Content:\n{article_context}"
                                )
                            }
                        ]

                        lnk_chat = ChatService(db, user=lnk_user)
                        lnk_chat.num_predict = min(lnk_chat.num_predict, 900)
                        post_text = await _asyncio.wait_for(lnk_chat.chat(post_messages), timeout=120)
                        post_text = _strip_hashtags(post_text).rstrip() + f"\n\n{cached_url}"
                        await _offer_social_post(chat_id, post_text, lnk_user, telegram_service)
                    except _asyncio.TimeoutError:
                        await telegram_service.send_message(chat_id, "Timed out generating post.")
                    except Exception as lnk_err:
                        logger.error(f"Link post generation error: {lnk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error generating post: {lnk_err}")

                elif action == "screenshot":
                    await telegram_service.send_message(chat_id, "⏳ Capturing screenshot, please wait...")
                    try:
                        lnk_cmd_service = CommandService(db, user=lnk_user)
                        shot_result = await lnk_cmd_service.execute_command("screenshot", cached_url)
                        if shot_result.get("type") == "generated_image" and shot_result.get("image"):
                            await _send_screenshot(chat_id, shot_result["image"], shot_result.get("content", cached_url))
                        else:
                            # error text from the command (e.g. Firefox missing / capture failed)
                            await telegram_service.send_message(chat_id, shot_result.get("content", "Screenshot failed."))
                    except Exception as lnk_err:
                        logger.error(f"Link screenshot error: {lnk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error capturing screenshot: {lnk_err}")

            elif data.startswith("yt:"):
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
                        from app.models import Setting as _Setting
                        _cookies_s = db.query(_Setting).filter(_Setting.key == "ytdl_cookies_path").first()
                        _cookies_path = str(_cookies_s.value).strip() if _cookies_s and _cookies_s.value else None
                        if _cookies_path and not _os.path.isfile(_cookies_path):
                            _cookies_path = None
                        _ssl_s = db.query(_Setting).filter(_Setting.key == "ytdl_no_ssl_verify").first()
                        _no_ssl = (
                            str(_ssl_s.value).strip().lower() in ("true", "1", "yes")
                            if _ssl_s and _ssl_s.value else False
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
                            from app.models import Setting as _Setting
                            _cookies_s = db.query(_Setting).filter(_Setting.key == "ytdl_cookies_path").first()
                            _cookies_path = str(_cookies_s.value).strip() if _cookies_s and _cookies_s.value else None
                            if _cookies_path and not _os.path.isfile(_cookies_path):
                                _cookies_path = None
                            _ssl_s = db.query(_Setting).filter(_Setting.key == "ytdl_no_ssl_verify").first()
                            _no_ssl = (
                                str(_ssl_s.value).strip().lower() in ("true", "1", "yes")
                                if _ssl_s and _ssl_s.value else False
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

            elif data.startswith("nk:"):
                # News → Post to Misskey: nk:post:<article_number>
                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""

                if action == "post" and len(parts) >= 3:
                    try:
                        article_num = int(parts[2])
                    except ValueError:
                        return {"ok": True}

                    cached_articles = _news_post_cache.get(chat_id)
                    if not cached_articles or article_num < 1 or article_num > len(cached_articles):
                        await telegram_service.send_message(chat_id, "⚠️ News article not found. Fetch the news again and try.")
                        return {"ok": True}

                    title, url = cached_articles[article_num - 1]

                    nk_user = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()

                    if not nk_user or (not _has_misskey(nk_user) and not _has_pleroma(nk_user) and not _has_matrix(nk_user)):
                        await telegram_service.send_message(chat_id, "⚠️ No social platform (Misskey, Pleroma, or Matrix) configured on your account.")
                        return {"ok": True}

                    await telegram_service.send_message(chat_id, f"⏳ Generating social media post for: {title}")

                    try:
                        from app.services.search_service import SearchService as _SS
                        import asyncio as _asyncio
                        _ss = _SS(db)
                        fetched = await _asyncio.wait_for(_ss.fetch_urls([url], max_urls=1), timeout=15)
                        article_context = f"Title: {title}\n\n{url}"
                        if fetched and fetched[0].get("content") and not fetched[0].get("error"):
                            article_context = f"Title: {fetched[0].get('title', title)}\n\n{fetched[0]['content'][:3000]}"

                        nk_chat = ChatService(db, user=nk_user)
                        nk_chat.num_predict = min(nk_chat.num_predict, 900)
                        post_messages = [
                            {
                                "role": "system",
                                "content": "You are a social media expert. Write compelling, detailed social media posts. Output ONLY the post text. No introductions, no 'here is your post', no URL placeholders like 'link' or 'read more'."
                            },
                            {
                                "role": "user",
                                "content": (
                                    "Write a viral and engaging social media post based on this news article. "
                                    "Be detailed — include key facts, context, and why it matters. "
                                    "Use emojis.\n\n"
                                    f"Content:\n{article_context}"
                                )
                            }
                        ]
                        post_text = await nk_chat.chat(post_messages)
                        post_text = _strip_hashtags(post_text).rstrip() + f"\n\n{url}"
                        await _offer_social_post(chat_id, post_text, nk_user, telegram_service)
                    except Exception as nk_err:
                        logger.error(f"News social post generation error: {nk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Error generating post: {nk_err}")

            elif data == "glow:textpost":
                # Render the pending post text as a glowing neon graphic, then re-offer
                # the SAME share buttons with it attached. Reuses the standard image
                # plumbing (_geni_image_cache, which every platform post handler reads),
                # so nothing about the existing post/share workflow changes — the text
                # body and platform targets are untouched, just an image gets added.
                _gp = (_misskey_post_cache.get(chat_id) or _pleroma_post_cache.get(chat_id)
                       or _matrix_post_cache.get(chat_id))
                if _gp in (None, _CONSUMED):
                    _gp = _recover_post_text(callback_query) or None
                if not _gp:
                    await telegram_service.send_message(chat_id, "No post text to glow — generate a post first.")
                    return {"ok": True}
                _gu = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True,
                ).first()
                # A URL baked into the glow image is useless (not clickable) — keep links OUT of
                # the image and put them in the post body instead.
                import re as _re
                _glow_urls = _re.findall(r'https?://\S+', _gp)
                _glow_text = _re.sub(r'https?://\S+', '', _gp).strip()
                try:
                    from app.services import effects_service as _fx
                    _glow_png = await asyncio.to_thread(_fx.render_glow_text_card, _glow_text or _gp)
                except Exception as _ge:
                    logger.error(f"glow text card failed: {_ge}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Couldn't render the glowing text: {_ge}")
                    return {"ok": True}
                # send_photo takes a str (URL/path/base64), not raw bytes — encode for the
                # preview. _offer_social_post keeps the RAW bytes (the platform post
                # handlers attach _geni_image_cache as raw image_bytes).
                import base64 as _b64
                await telegram_service.send_photo(
                    chat_id, _b64.b64encode(_glow_png).decode("ascii"), "🌟 Glowing text preview")
                # The glowing TEXT is now the image; keep any link(s) in the post body so they
                # stay clickable (don't re-post the text — it's in the image).
                _glow_body = "\n".join(_glow_urls)
                await _offer_social_post(chat_id, _glow_body, _gu, telegram_service,
                                         prompt="📣 *Post this glowing image?*", image_bytes=_glow_png)
                return {"ok": True}

            elif data.startswith("mk:"):
                action = data.split(":", 1)[1]

                if action == "skip":
                    # Clear all social post caches so stale posts can't be sent
                    _misskey_post_cache.pop(chat_id, None)
                    _pleroma_post_cache.pop(chat_id, None)
                    _matrix_post_cache.pop(chat_id, None)
                    _matrix_room_cache.pop(chat_id, None)
                    _geni_image_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "Post skipped.")
                    return {"ok": True}

                # action == "post"
                pending_post = _misskey_post_cache.pop(chat_id, None)
                if pending_post == _CONSUMED:
                    await telegram_service.send_message(chat_id, "Already posted via 'Post to All'.")
                    return {"ok": True}
                if pending_post is None:
                    pending_post = _recover_post_text(callback_query) or None
                if pending_post is None:
                    await telegram_service.send_message(chat_id, "No pending Misskey post found. Please generate a new post.")
                    return {"ok": True}

                mk_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if (
                    not mk_user
                    or not getattr(mk_user, "misskey_enabled", False)
                    or not getattr(mk_user, "misskey_instance_url", None)
                    or not getattr(mk_user, "misskey_api_token", None)
                ):
                    await telegram_service.send_message(chat_id, "Misskey is not configured on your account.")
                    return {"ok": True}

                _mk_image = _geni_image_cache.get(chat_id)  # .get so other platforms can still use it
                try:
                    from app.services.misskey_service import post_note as _misskey_post_note
                    await _misskey_post_note(
                        mk_user.misskey_instance_url,
                        mk_user.misskey_api_token,
                        pending_post,
                        image_bytes=_mk_image,
                    )
                    await telegram_service.send_message(chat_id, "✅ Posted to Misskey!")
                except Exception as mk_err:
                    logger.error(f"Misskey post error: {mk_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Failed to post to Misskey: {mk_err}")

            elif data.startswith("plr:"):
                action = data.split(":", 1)[1]

                if action == "skip":
                    _misskey_post_cache.pop(chat_id, None)
                    _pleroma_post_cache.pop(chat_id, None)
                    _matrix_post_cache.pop(chat_id, None)
                    _matrix_room_cache.pop(chat_id, None)
                    _geni_image_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "Post skipped.")
                    return {"ok": True}

                # action == "post"
                pending_post = _pleroma_post_cache.pop(chat_id, None)
                if pending_post == _CONSUMED:
                    await telegram_service.send_message(chat_id, "Already posted via 'Post to All'.")
                    return {"ok": True}
                if pending_post is None:
                    pending_post = _recover_post_text(callback_query) or None
                if pending_post is None:
                    await telegram_service.send_message(chat_id, "No pending Pleroma post found. Please generate a new post.")
                    return {"ok": True}

                plr_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if not plr_user or not _has_pleroma(plr_user):
                    await telegram_service.send_message(chat_id, "Pleroma is not configured on your account.")
                    return {"ok": True}

                _plr_image = _geni_image_cache.get(chat_id)  # .get so other platforms can still use it
                try:
                    from app.services.pleroma_service import post_status as _pleroma_post_status
                    await _pleroma_post_status(
                        plr_user.pleroma_instance_url,
                        plr_user.pleroma_access_token,
                        pending_post,
                        image_bytes=_plr_image,
                    )
                    await telegram_service.send_message(chat_id, "✅ Posted to Pleroma!")
                except Exception as plr_err:
                    logger.error(f"Pleroma post error: {plr_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Failed to post to Pleroma: {plr_err}")

            elif data.startswith("mtx:"):
                # Matrix post flow:
                # mtx:post   → fetch rooms, show room selector
                # mtx:room:N → send to room N (index into _matrix_room_cache[chat_id])
                parts = data.split(":", 2)
                action = parts[1] if len(parts) > 1 else ""

                mtx_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if action == "post":
                    pending_post = _matrix_post_cache.get(chat_id)
                    # Cache miss (e.g. after service restart) — recover from message text.
                    # "" is a valid caption-less media post, so only recover when truly absent.
                    if pending_post is None:
                        pending_post = _recover_post_text(callback_query) or None
                        if pending_post:
                            _matrix_post_cache[chat_id] = pending_post
                            logger.info(f"mtx:post — recovered post text from message ({len(pending_post)} chars)")
                    if pending_post is None:
                        await telegram_service.send_message(chat_id, "No pending Matrix post found. Please generate a new post.")
                        return {"ok": True}

                    if not mtx_user or not _has_matrix(mtx_user):
                        await telegram_service.send_message(chat_id, "Matrix is not configured on your account.")
                        return {"ok": True}

                    # Fetch rooms
                    try:
                        from app.services.matrix_service import get_joined_rooms as _mtx_rooms
                        rooms = await _mtx_rooms(mtx_user.matrix_homeserver, mtx_user.matrix_access_token)
                    except Exception as mtx_err:
                        logger.error(f"Matrix fetch rooms error: {mtx_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Could not fetch Matrix rooms: {mtx_err}")
                        return {"ok": True}

                    if not rooms:
                        await telegram_service.send_message(chat_id, "⚠️ No Matrix rooms found. Join a room first.")
                        return {"ok": True}

                    _matrix_room_cache[chat_id] = rooms

                    # Build room selector keyboard (up to 20 rooms, 2 per row)
                    buttons = []
                    row: list = []
                    for i, room in enumerate(rooms[:20]):
                        label = room["name"][:30]
                        row.append({"text": label, "callback_data": f"mtx:room:{i}"})
                        if len(row) == 2:
                            buttons.append(row)
                            row = []
                    if row:
                        buttons.append(row)
                    buttons.append([{"text": "❌ Cancel", "callback_data": "mtx:cancel"}])

                    await telegram_service.send_message(
                        chat_id,
                        "📬 Which Matrix room do you want to post to?",
                        reply_markup={"inline_keyboard": buttons},
                    )

                elif action == "room" and len(parts) >= 3:
                    try:
                        room_idx = int(parts[2])
                    except ValueError:
                        return {"ok": True}

                    # Pop only the matrix caches — leave Misskey/Pleroma caches intact
                    # so the user can still post to those platforms after choosing a Matrix room
                    pending_post = _matrix_post_cache.pop(chat_id, None)
                    rooms = _matrix_room_cache.pop(chat_id, [])

                    # "" is a valid caption-less media post; only bail when truly absent.
                    if pending_post is None:
                        await telegram_service.send_message(chat_id, "No pending Matrix post found.")
                        return {"ok": True}

                    if not rooms or room_idx < 0 or room_idx >= len(rooms):
                        await telegram_service.send_message(chat_id, "Room not found. Please try again.")
                        return {"ok": True}

                    if not mtx_user or not _has_matrix(mtx_user):
                        await telegram_service.send_message(chat_id, "Matrix is not configured on your account.")
                        return {"ok": True}

                    room = rooms[room_idx]
                    image_bytes = _geni_image_cache.pop(chat_id, None)
                    try:
                        if image_bytes:
                            from app.services.matrix_service import send_image as _mtx_send_img
                            await _mtx_send_img(
                                mtx_user.matrix_homeserver,
                                mtx_user.matrix_access_token,
                                room["room_id"],
                                image_bytes,
                                caption=pending_post,
                            )
                        else:
                            from app.services.matrix_service import send_message as _mtx_send
                            await _mtx_send(
                                mtx_user.matrix_homeserver,
                                mtx_user.matrix_access_token,
                                room["room_id"],
                                pending_post,
                            )
                        await telegram_service.send_message(chat_id, f"✅ Posted to Matrix room: {room['name']}")
                    except Exception as mtx_err:
                        logger.error(f"Matrix send error: {mtx_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Failed to post to Matrix: {mtx_err}")

                elif action == "cancel":
                    _matrix_post_cache.pop(chat_id, None)
                    _matrix_room_cache.pop(chat_id, None)
                    _misskey_post_cache.pop(chat_id, None)
                    _pleroma_post_cache.pop(chat_id, None)
                    _geni_image_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "Post cancelled.")

            elif data == "all:post":
                # Post to every configured platform simultaneously.
                # Misskey + Pleroma are posted right away; Matrix shows room selector.
                all_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                # Recover post text from message if caches were lost (e.g. service restart).
                # Shared _recover_post_text() strips the prompt and refuses to post a bare
                # prompt — same helper used by the individual mk:/plr:/mtx: handlers.

                results = []
                matrix_attempted = False

                _all_image = _geni_image_cache.get(chat_id)  # leave in cache for Matrix room-picker step

                # Misskey — post when there's text OR a media attachment (caption-less post).
                mk_post = _misskey_post_cache.pop(chat_id, None) or _recover_post_text(callback_query)
                if (mk_post or _all_image) and all_user and _has_misskey(all_user):
                    try:
                        from app.services.misskey_service import post_note as _mk_note
                        await _mk_note(all_user.misskey_instance_url, all_user.misskey_api_token, mk_post,
                                       image_bytes=_all_image)
                        results.append("✅ Misskey")
                    except Exception as _e:
                        logger.error(f"all:post Misskey error: {_e}", exc_info=True)
                        results.append(f"❌ Misskey: {_e}")
                    # Sentinel prevents old Misskey button from double-posting
                    _misskey_post_cache[chat_id] = _CONSUMED

                # Pleroma
                plr_post = _pleroma_post_cache.pop(chat_id, None) or _recover_post_text(callback_query)
                if (plr_post or _all_image) and all_user and _has_pleroma(all_user):
                    try:
                        from app.services.pleroma_service import post_status as _plr_status
                        await _plr_status(all_user.pleroma_instance_url, all_user.pleroma_access_token, plr_post,
                                          image_bytes=_all_image)
                        results.append("✅ Pleroma")
                    except Exception as _e:
                        logger.error(f"all:post Pleroma error: {_e}", exc_info=True)
                        results.append(f"❌ Pleroma: {_e}")
                    # Sentinel prevents old Pleroma button from double-posting
                    _pleroma_post_cache[chat_id] = _CONSUMED

                if results:
                    await telegram_service.send_message(chat_id, "\n".join(results))

                # Matrix — needs room selection; show picker if configured
                mtx_post = _matrix_post_cache.get(chat_id) or _recover_post_text(callback_query)
                if (mtx_post or _all_image) and mtx_post != _CONSUMED and all_user and _has_matrix(all_user):
                    matrix_attempted = True
                    try:
                        from app.services.matrix_service import get_joined_rooms as _mtx_rooms
                        rooms = await _mtx_rooms(all_user.matrix_homeserver, all_user.matrix_access_token)
                        if rooms:
                            _matrix_room_cache[chat_id] = rooms
                            if mtx_post != _matrix_post_cache.get(chat_id):
                                _matrix_post_cache[chat_id] = mtx_post
                            btns = []
                            row: list = []
                            for i, room in enumerate(rooms[:20]):
                                row.append({"text": room["name"][:30], "callback_data": f"mtx:room:{i}"})
                                if len(row) == 2:
                                    btns.append(row)
                                    row = []
                            if row:
                                btns.append(row)
                            btns.append([{"text": "❌ Skip Matrix", "callback_data": "mtx:cancel"}])
                            await telegram_service.send_message(
                                chat_id,
                                "📬 Which Matrix room?",
                                reply_markup={"inline_keyboard": btns},
                            )
                        else:
                            _matrix_post_cache.pop(chat_id, None)
                            await telegram_service.send_message(chat_id, "⚠️ No Matrix rooms found — skipped.")
                    except Exception as _e:
                        logger.error(f"all:post Matrix rooms error: {_e}", exc_info=True)
                        _matrix_post_cache.pop(chat_id, None)
                        await telegram_service.send_message(chat_id, f"❌ Matrix room fetch failed: {_e}")

                if not results and not matrix_attempted:
                    await telegram_service.send_message(chat_id, "No social platforms configured.")

            return {"ok": True}

        return {"ok": True}
    
    except Exception as e:
        logger.error(f"Error processing Telegram webhook: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


@router.post("/test")
async def test_telegram_connection(
    data: TelegramBotConfig,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Test Telegram bot connection."""
    if not data.bot_token:
        raise HTTPException(status_code=400, detail="Bot token required")
    
    telegram_service.set_token(data.bot_token)
    _configure_telegram(db)
    result = await telegram_service.get_me()

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to connect to Telegram"))
    
    bot_info = result.get("result", {})
    return {
        "ok": True,
        "bot": {
            "id": bot_info.get("id"),
            "username": bot_info.get("username"),
            "first_name": bot_info.get("first_name")
        }
    }


@router.post("/test-local-api")
async def test_local_api(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Ping the configured local Bot API server (getMe) to verify it's reachable
    and the bot is registered there — useful before enabling local mode."""
    base = db.query(Setting).filter(Setting.key == "telegram_api_base").first()
    token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
    if not base or not base.value:
        raise HTTPException(status_code=400, detail="Set and save the Bot API server URL first.")
    if not token or not token.value:
        raise HTTPException(status_code=400, detail="Telegram bot token not configured.")

    from app.services.telegram_service import TelegramService
    svc = TelegramService(token.value)
    svc.set_api_base(base.value)
    try:
        result = await svc.get_me()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not reach {base.value}: {e}")
    if not result.get("ok"):
        detail = result.get("error") or result.get("description") or \
            "Reached the server, but the bot isn't registered there yet (run the setup script; after a cloud logOut it can take ~10 min)."
        raise HTTPException(status_code=400, detail=detail)

    info = result.get("result", {})
    return {
        "ok": True,
        "api_base": svc.api_root,
        "bot": {"username": info.get("username"), "first_name": info.get("first_name")},
    }


@router.post("/set-webhook")
async def configure_webhook(
    data: TelegramBotConfig,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Configure Telegram bot webhook."""
    logger.info(f"configure_webhook called with bot_token={'***' if data.bot_token else None}, webhook_url={data.webhook_url}")
    
    # First, save the token if provided
    if data.bot_token:
        setting = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
        if setting:
            setting.value = data.bot_token
        else:
            db.add(Setting(key="telegram_bot_token", value=data.bot_token))
        db.commit()
        telegram_service.set_token(data.bot_token)
    else:
        bot_token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
        if not bot_token or not bot_token.value:
            raise HTTPException(status_code=400, detail="Telegram bot token not configured")
        telegram_service.set_token(bot_token.value)

    # Register the webhook with the local Bot API server when enabled, else cloud.
    _configure_telegram(db)

    if data.webhook_url:
        logger.info(f"Calling set_webhook with URL: {data.webhook_url}")
        result = await telegram_service.set_webhook(data.webhook_url)
        logger.info(f"set_webhook result: {result}")
    else:
        result = await telegram_service.delete_webhook()
    
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to configure webhook"))
    
    return result


@router.get("/users")
async def list_telegram_users(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """List users with Telegram enabled."""
    users = db.query(User).filter(
        User.telegram_enabled == True,
        User.telegram_chat_id.isnot(None)
    ).all()
    
    return [
        {
            "id": u.id,
            "username": u.username,
            "telegram_chat_id": u.telegram_chat_id,
            "telegram_notifications": u.telegram_notifications
        }
        for u in users
    ]


@router.post("/generate-key")
async def generate_telegram_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate a one-time key the user sends to the bot via /start to link their account."""
    import secrets
    from datetime import datetime, timedelta
    previous_key_revoked = bool(current_user.telegram_key)
    key = secrets.token_urlsafe(32)
    current_user.telegram_key = key
    current_user.telegram_key_expires_at = datetime.utcnow() + timedelta(hours=24)
    db.commit()
    db.refresh(current_user)
    return {
        "ok": True,
        "key": key,
        "expires_at": current_user.telegram_key_expires_at.isoformat(),
        "previous_key_revoked": previous_key_revoked,
    }


@router.delete("/generate-key")
async def revoke_telegram_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Revoke (clear) the pending Telegram link key."""
    current_user.telegram_key = None
    current_user.telegram_key_expires_at = None
    db.commit()
    db.refresh(current_user)
    return {"ok": True}


@router.post("/link")
async def link_telegram_chat(
    data: TelegramChatSetup,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Link current user's account to a Telegram chat."""
    from sqlalchemy.exc import IntegrityError
    current_user.telegram_chat_id = data.chat_id
    current_user.telegram_enabled = True
    current_user.telegram_notifications = data.notifications
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="That Telegram chat ID is already linked to another account.")
    return {"ok": True, "message": f"Linked to chat {data.chat_id}"}


@router.post("/unlink")
async def unlink_telegram_chat(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Unlink current user's Telegram account."""
    current_user.telegram_enabled = False
    current_user.telegram_chat_id = None
    current_user.telegram_notifications = ""
    current_user.telegram_key = None
    current_user.telegram_key_expires_at = None

    db.commit()
    
    return {"ok": True, "message": "Telegram account unlinked"}


@router.post("/broadcast")
async def broadcast_to_telegram_users(
    message: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Broadcast a message to all users with Telegram enabled."""
    bot_token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
    if not bot_token or not bot_token.value:
        raise HTTPException(status_code=400, detail="Telegram bot not configured")
    
    telegram_service.set_token(bot_token.value)
    _configure_telegram(db)

    users = db.query(User).filter(
        User.telegram_enabled == True,
        User.telegram_chat_id.isnot(None)
    ).all()
    
    results = []
    for user in users:
        try:
            result = await telegram_service.send_message(user.telegram_chat_id, message)
            results.append({"user_id": user.id, "ok": result.get("ok", False)})
        except Exception as e:
            logger.error(f"Failed to send message to user {user.id}: {e}")
            results.append({"user_id": user.id, "ok": False, "error": str(e)})
    
    return {"results": results}
