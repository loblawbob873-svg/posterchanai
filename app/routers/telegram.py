from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import logging
import json
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
from app.services.telegram_service import telegram_service
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
                {"text": "🎨 Image Gen",   "callback_data": "help:geni"},
                {"text": "🧲 Torrents",    "callback_data": "t:menu"},
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
        ]
    }


_HELP_SECTIONS = {
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
        "Reply to any message containing a link or text with `post` to generate a social media post\\.\n\n"
        "• Add a tone modifier after `post` to style the output\n\n"
        "*Examples:*\n"
        "\\(reply to a news link\\) `post`\n"
        "\\(reply to an article\\) `post professional`\n"
        "\\(reply to a link\\) `post funny`"
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
# Pending link actions: chat_id → url (cleared once action is chosen)
_link_action_cache: dict = {}



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


async def _offer_social_post(chat_id: str, post_text: str, user, telegram_svc, prompt: str = "📣 *Post this?*"):
    """Show the generated post and offer to share it on configured social platforms."""
    buttons = []
    has_mk = _has_misskey(user)
    has_plr = _has_pleroma(user)
    has_mtx = _has_matrix(user)

    if has_mk:
        _misskey_post_cache[chat_id] = post_text
        buttons.append({"text": "📣 Misskey", "callback_data": "mk:post"})
    if has_plr:
        _pleroma_post_cache[chat_id] = post_text
        buttons.append({"text": "📣 Pleroma", "callback_data": "plr:post"})
    if has_mtx:
        _matrix_post_cache[chat_id] = post_text
        buttons.append({"text": "📣 Matrix", "callback_data": "mtx:post"})

    if buttons:
        buttons.append({"text": "❌ Skip", "callback_data": "mk:skip"})
        await telegram_svc.send_message(
            chat_id,
            post_text + f"\n\n{prompt}",
            reply_markup={"inline_keyboard": [buttons]},
        )
    else:
        await telegram_svc.send_message(chat_id, post_text)
# Pending YouTube actions: chat_id → url (cleared once action is chosen)
_youtube_action_cache: dict = {}
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
            }
            reply_from = (reply_to or {}).get("from", {})
            if reply_from.get("is_bot") and text.strip():
                route = _FORCE_REPLY_ROUTES.get(reply_text.strip())
                if route:
                    text = f"{route} {text.strip()}"
                    text_lower = text.lower()
                    reply_to = {}
                    reply_text = ""

            # Detect forwarded messages
            is_forwarded = bool(
                message.get("forward_date") or
                message.get("forward_origin") or
                message.get("forward_from") or
                message.get("forward_from_chat")
            )
            
            # Check for attachments (photos, documents)
            # Photos in Telegram messages are in a list - get the highest res (last one)
            photos = message.get("photo", [])
            document = message.get("document", [])
            
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
            commands = ["help", "new", "ytdl", "geni", "mail", "news", "search", "images", "yt", "torrents", "nyaa", "4chan", "logs", "translate", "post"]
            for cmd in commands:
                if text_lower.startswith(cmd + " ") or text_lower == cmd:
                    command = cmd
                    arg = text[len(cmd):].strip()
                    break

            # "post" can appear anywhere in a short reply message (e.g. "send post", "make a post")
            if command is None and reply_to and len(text_lower.split()) <= 5 and "post" in text_lower:
                command = "post"
                # Only use words AFTER "post" as tone modifier (e.g. "post professional" → "professional")
                parts = text_lower.split("post", 1)
                arg = parts[1].strip() if len(parts) > 1 else ""

            # If it's a reply and translate command, handle it
            if reply_text and command == "translate":
                logger.warning(f"TRANSLATE: Processing reply with text: {reply_text[:100]}...")
                # Use the replied text for translation
                language = arg.replace("to", "").strip() or "English"
                
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
                    await telegram_service.send_message(chat_id, "Reply to a message containing a link and send `post` to generate a social media post.")
                    return {"ok": True}

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

                tone = arg.strip() or "viral and engaging"
                post_messages = [
                    {
                        "role": "system",
                        "content": "You are a social media expert. Write compelling, detailed social media posts. Output ONLY the post text. No introductions, no 'here is your post', no URL placeholders like 'link' or 'read more'."
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Write a {tone} social media post based on this content. "
                            f"Be detailed — include key facts, context, and why it matters. "
                            f"Use emojis and relevant hashtags. Stop after the last hashtag.\n\n"
                            f"Content:\n{article_context}"
                        )
                    }
                ]

                from app.services.chat_service import ChatService as _CS
                _cs = _CS(db, user=None)
                _cs.num_predict = min(_cs.num_predict, 900)
                try:
                    post_text = await _cs.chat(post_messages)
                    # Always append the real URL at the end — the model may mangle it
                    if url_to_append:
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
            
            # Check if the message starts with a known command
            command = None
            arg = text
            commands = ["help", "new", "ytdl", "geni", "mail", "news", "search", "images", "yt", "torrents", "nyaa", "4chan", "logs", "translate", "post"]
            for cmd in commands:
                if text_lower.startswith(cmd + " ") or text_lower == cmd:
                    command = cmd
                    arg = text[len(cmd):].strip()
                    break

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
                if file_id:
                    logger.info(f"Processing document: {file_name}")
                    file_result = await telegram_service.get_file(file_id)
                    if file_result.get("ok"):
                        file_path = file_result.get("result", {}).get("file_path")
                        if file_path:
                            downloaded_data = await telegram_service.download_file(file_path)
                            if downloaded_data:
                                # Determine content type
                                content_type = "application/octet-stream"
                                if file_name.endswith('.pdf'):
                                    content_type = "application/pdf"
                                elif file_name.endswith(('.jpg', '.jpeg')):
                                    content_type = "image/jpeg"
                                elif file_name.endswith('.png'):
                                    content_type = "image/png"
                                elif file_name.endswith('.gif'):
                                    content_type = "image/gif"
                                attachments.append((file_name, downloaded_data, content_type))
                                logger.info(f"Downloaded document: {file_name}, size: {len(downloaded_data)}")

            # Handle Telegram media groups: multiple docs sent together arrive as separate webhooks
            # with the same media_group_id. Accumulate them before processing.
            media_group_id = message.get("media_group_id")
            if media_group_id and attachments:
                if media_group_id not in _MEDIA_GROUP_CACHE:
                    _MEDIA_GROUP_CACHE[media_group_id] = {
                        "attachments": [], "text": "", "created_at": time.time()
                    }
                _mg = _MEDIA_GROUP_CACHE[media_group_id]
                if text.strip():
                    _mg["text"] = text  # use caption from whichever message has it
                _mg["attachments"].extend(attachments)
                # Wait briefly for remaining messages in this group to arrive
                await asyncio.sleep(2.0)
                # Only first handler to pop processes; others return early
                _mg_data = _MEDIA_GROUP_CACHE.pop(media_group_id, None)
                if _mg_data is None:
                    return {"ok": True}
                attachments = _mg_data["attachments"]
                text = _mg_data["text"] or text
                text_lower = text.lower().strip()
                logger.info(f"[MEDIA-GROUP] {media_group_id}: assembled {len(attachments)} attachments, text={text!r}")

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
            if has_images and attachments:
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
            
            reply_markup = None
            if command:
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
                        tg_conv = db.query(Conversation).filter(
                            Conversation.user_id == user_obj.id,
                            Conversation.title == "📱 Telegram"
                        ).order_by(Conversation.updated_at.desc()).first()
                        if tg_conv:
                            db.query(Message).filter(Message.conversation_id == tg_conv.id).delete()
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

                                file_size = _os.path.getsize(dl_result.local_path)
                                # Telegram bot limit is 50 MB for videos
                                if file_size > 50 * 1024 * 1024:
                                    # File too large - save to storage and notify
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

                                # Send the video
                                duration_int = int(dl_result.duration) if dl_result.duration else None
                                caption = f"🎬 **{dl_result.title}**" if dl_result.title else "🎬 Video"
                                if dl_result.artist:
                                    caption += f"\n👤 {dl_result.artist}"

                                video_result = await telegram_service.send_video(
                                    chat_id=chat_id,
                                    file_path=dl_result.local_path,
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
                    else:
                        # Pass attachments to any command that supports them
                        if attachments:
                            result = await command_service.execute_command(command, arg, attachments=attachments)
                        else:
                            result = await command_service.execute_command(command, arg)
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
                                "inline_keyboard": [[
                                    {"text": "📋 Summary", "callback_data": "lnk:summary"},
                                    {"text": "📣 Post",    "callback_data": "lnk:post"},
                                    {"text": "❌ Cancel",  "callback_data": "lnk:cancel"},
                                ]]
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
                                "inline_keyboard": [[
                                    {"text": "📋 Summary", "callback_data": "lnk:summary"},
                                    {"text": "📣 Post",    "callback_data": "lnk:post"},
                                    {"text": "❌ Cancel",  "callback_data": "lnk:cancel"},
                                ]]
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
            
            if response_type == "generated_image" and image_data:
                logger.info(f"Generated image detected, sending via Telegram, image length: {len(image_data)}")
                photo_result = await telegram_service.send_photo(chat_id, image_data, response_content)
                if not photo_result.get("ok"):
                    logger.error(f"Failed to send photo: {photo_result}")
                    await telegram_service.send_message(chat_id, f"{response_content}\n\n(Image generation failed to send)")
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
                                {"role": "system", "content": "Generate a short, engaging social media post (under 280 characters) for this news article. Include relevant hashtags."},
                                {"role": "user", "content": f"Title: {title}\nURL: {url}\n\nGenerate a social media post."}
                            ]
                            post_text = await chat_service.chat(messages)
                            await _offer_social_post(chat_id, post_text, cb_user, telegram_service)
                        else:
                            await telegram_service.send_message(chat_id, "❌ Source not found. Please try again.")
                    except (ValueError, IndexError):
                        await telegram_service.send_message(chat_id, "❌ Invalid source selection.")
                    return {"ok": True}

            elif data.startswith("prompt:"):
                action = data.split(":", 1)[1]
                _PROMPT_CONFIGS = {
                    "search":   ("🔍 What would you like to search for?", "e.g. latest AI news"),
                    "images":   ("🖼 What images would you like to search for?", "e.g. northern lights"),
                    "geni":     ("🎨 Describe the image you want to generate:", "e.g. a sunset over a cyberpunk city"),
                    "nyaa":     ("🔎 Type your anime search:", "e.g. one piece 1080p"),
                    "torrents": ("🔍 Type your torrent search:", "e.g. dark knight 1080p"),
                    "4chan":    ("🍀 Which board? (g, pol, a, or h)", "e.g. g"),
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
                        from app.services.search_service import SearchService as _SS
                        import asyncio as _asyncio
                        _ss = _SS(db)
                        fetched = await _asyncio.wait_for(_ss.fetch_urls([cached_url], max_urls=1), timeout=15)
                        if fetched and fetched[0].get("content") and not fetched[0].get("error"):
                            content = fetched[0]["content"][:4000]
                            title = fetched[0].get("title", "")
                            lnk_chat = ChatService(db, user=lnk_user)
                            summary_msgs = [
                                {"role": "system", "content": "You are a thorough summarizer. Output only the summary, nothing else. No introductions or meta-commentary."},
                                {"role": "user", "content": f"Title: {title}\n\n{content}\n\nWrite a detailed summary of the above. Include the key points, important facts, context, and any notable details. Use bullet points where helpful."}
                            ]
                            summary = await _asyncio.wait_for(lnk_chat.chat(summary_msgs), timeout=120)
                            await telegram_service.send_message(chat_id, summary)
                        else:
                            error_detail = fetched[0].get("error", "") if fetched else "Could not reach URL"
                            msg = "Could not fetch content from the URL."
                            if error_detail:
                                msg += f" ({error_detail})"
                            await telegram_service.send_message(chat_id, msg)
                    except _asyncio.TimeoutError:
                        await telegram_service.send_message(chat_id, "Timed out fetching or summarizing the link.")
                    except Exception as lnk_err:
                        logger.error(f"Link summary error: {lnk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error: {lnk_err}")

                elif action == "post":
                    await telegram_service.send_message(chat_id, "⏳ Generating post, please wait...")
                    try:
                        from app.services.search_service import SearchService as _SS
                        import asyncio as _asyncio
                        _ss = _SS(db)
                        fetched = await _asyncio.wait_for(_ss.fetch_urls([cached_url], max_urls=1), timeout=15)
                        article_context = cached_url
                        if fetched and fetched[0].get("content") and not fetched[0].get("error"):
                            article_context = f"Title: {fetched[0].get('title', '')}\n\n{fetched[0]['content'][:3000]}"

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
                                    "Use emojis and relevant hashtags. Stop after the last hashtag.\n\n"
                                    f"Content:\n{article_context}"
                                )
                            }
                        ]

                        lnk_chat = ChatService(db, user=lnk_user)
                        lnk_chat.num_predict = min(lnk_chat.num_predict, 900)
                        post_text = await _asyncio.wait_for(lnk_chat.chat(post_messages), timeout=120)
                        post_text = post_text.rstrip() + f"\n\n{cached_url}"
                        await _offer_social_post(chat_id, post_text, lnk_user, telegram_service)
                    except _asyncio.TimeoutError:
                        await telegram_service.send_message(chat_id, "Timed out generating post.")
                    except Exception as lnk_err:
                        logger.error(f"Link post generation error: {lnk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error generating post: {lnk_err}")

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
                                    "Use emojis and relevant hashtags. Stop after the last hashtag.\n\n"
                                    f"Content:\n{video_context}"
                                )
                            }
                        ]
                        post_text = await yt_chat.chat(post_messages)
                        post_text = post_text.rstrip() + f"\n\n{yt_url}"
                        await _offer_social_post(chat_id, post_text, yt_user, telegram_service)
                    except Exception as yt_err:
                        logger.error(f"YouTube post generation error: {yt_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Error generating post: {yt_err}")

                elif action in ("mp3", "video"):
                    from app.services.youtube_service import (
                        check_ytdlp_available,
                        download_as_mp3,
                        download_video_and_save_to_storage,
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

                            file_size = _os.path.getsize(dl_result.local_path)
                            # Telegram bot limit is 50 MB for videos
                            if file_size > 50 * 1024 * 1024:
                                # File too large - save to storage and notify
                                save_result = await download_video_and_save_to_storage(
                                    url=yt_url,
                                    user_id=yt_user.id,
                                    db=db,
                                    subfolder="YouTube Videos",
                                )
                                from app.services.youtube_service import format_download_result
                                await telegram_service.send_message(
                                    chat_id,
                                    f"❌ Video is too large to send via Telegram ({file_size // (1024*1024)} MB).\n\n{format_download_result(save_result)}"
                                )
                                return {"ok": True}

                            # Send the video
                            duration_int = int(dl_result.duration) if dl_result.duration else None
                            caption = f"🎬 **{dl_result.title}**" if dl_result.title else "🎬 Video"
                            if dl_result.artist:
                                caption += f"\n👤 {dl_result.artist}"

                            video_result = await telegram_service.send_video(
                                chat_id=chat_id,
                                file_path=dl_result.local_path,
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
                                    "Use emojis and relevant hashtags. Stop after the last hashtag.\n\n"
                                    f"Content:\n{article_context}"
                                )
                            }
                        ]
                        post_text = await nk_chat.chat(post_messages)
                        post_text = post_text.rstrip() + f"\n\n{url}"
                        await _offer_social_post(chat_id, post_text, nk_user, telegram_service)
                    except Exception as nk_err:
                        logger.error(f"News social post generation error: {nk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Error generating post: {nk_err}")

            elif data.startswith("mk:"):
                action = data.split(":", 1)[1]

                if action == "skip":
                    # Clear all social post caches so stale posts can't be sent
                    _misskey_post_cache.pop(chat_id, None)
                    _pleroma_post_cache.pop(chat_id, None)
                    _matrix_post_cache.pop(chat_id, None)
                    _matrix_room_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "Post skipped.")
                    return {"ok": True}

                # action == "post"
                pending_post = _misskey_post_cache.pop(chat_id, None)
                if pending_post is None:
                    await telegram_service.send_message(chat_id, "No pending Misskey post found.")
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

                try:
                    from app.services.misskey_service import post_note as _misskey_post_note
                    await _misskey_post_note(
                        mk_user.misskey_instance_url,
                        mk_user.misskey_api_token,
                        pending_post,
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
                    await telegram_service.send_message(chat_id, "Post skipped.")
                    return {"ok": True}

                # action == "post"
                pending_post = _pleroma_post_cache.pop(chat_id, None)
                if pending_post is None:
                    await telegram_service.send_message(chat_id, "No pending Pleroma post found.")
                    return {"ok": True}

                plr_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if not plr_user or not _has_pleroma(plr_user):
                    await telegram_service.send_message(chat_id, "Pleroma is not configured on your account.")
                    return {"ok": True}

                try:
                    from app.services.pleroma_service import post_status as _pleroma_post_status
                    await _pleroma_post_status(
                        plr_user.pleroma_instance_url,
                        plr_user.pleroma_access_token,
                        pending_post,
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
                    if not pending_post:
                        await telegram_service.send_message(chat_id, "No pending Matrix post found.")
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

                    if not pending_post:
                        await telegram_service.send_message(chat_id, "No pending Matrix post found.")
                        return {"ok": True}

                    if not rooms or room_idx >= len(rooms):
                        await telegram_service.send_message(chat_id, "Room not found. Please try again.")
                        return {"ok": True}

                    if not mtx_user or not _has_matrix(mtx_user):
                        await telegram_service.send_message(chat_id, "Matrix is not configured on your account.")
                        return {"ok": True}

                    room = rooms[room_idx]
                    try:
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
                    await telegram_service.send_message(chat_id, "Post cancelled.")

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
