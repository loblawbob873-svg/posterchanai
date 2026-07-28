"""Auto-split from the original telegram.py monolith. No behavior change."""
from ._common import Optional, _FC_LETTERS, _FX_MEMES, _FX_SOUNDS, _FX_THEMES, _POST_PROMPTS, _TRANSLATE_LANGS, re

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
                {"text": "📸 Screenshot",  "callback_data": "prompt:screenshot"},
            ],
            [
                {"text": "⏰ Reminders",   "callback_data": "help:reminders"},
                {"text": "📌 Pins", "callback_data": "help:pins"},
            ],
        ]
    }


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



def _has_nostr(user) -> bool:
    # Nostr needs only a secret key (no instance); relays default if unset.
    return bool(
        user
        and getattr(user, "nostr_enabled", False)
        and getattr(user, "nostr_nsec", None)
    )


def _strip_hashtags(text: str) -> str:
    """Remove hashtag tokens from AI-generated post text (the model often ignores
    the 'no hashtags' instruction). Apply BEFORE appending any URL so URL fragments
    like example.com#section are never touched."""
    import re as _ht
    text = _ht.sub(r'(?:^|\s)#\w[\w-]*', ' ', text)
    return _ht.sub(r'[ \t]{2,}', ' ', text).strip()


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


def _character_prompt_keyboard() -> dict:
    """Buttons to pick a bottom-right character (or skip). Drives the media:chr:<name> callback."""
    return {"inline_keyboard": [
        [{"text": "🫵 Carl", "callback_data": "media:chr:carl"},
         {"text": "😮 Soyjak", "callback_data": "media:chr:soyjack"}],
        [{"text": "🙄 Anyways", "callback_data": "media:chr:anyways"},
         {"text": "🤷 Shrug", "callback_data": "media:chr:shrug"}],
        [{"text": "▶️ No character", "callback_data": "media:chr:none"}],
    ]}


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

    _social = bool(user and (_has_misskey(user) or _has_pleroma(user) or _has_nostr(user)))
    rows = []
    if has_video:
        rows.append([
            {"text": "🗜 Compress video", "callback_data": "media:compress"},
            {"text": "✂️ Clip video", "callback_data": "media:clip"},
            {"text": "🎵 Extract audio", "callback_data": "media:extractaudio"},
        ])
    if has_image:
        # Grouped by purpose so it reads as sections instead of one long column:
        # transform · read · create/learn.
        rows.append([
            {"text": "🗜 Compress", "callback_data": "media:compress"},
            {"text": "✂️ Remove BG", "callback_data": "media:removebackground"},
            {"text": "⭕ Circle crop", "callback_data": "media:circlecrop"},
            {"text": "📄 To PDF", "callback_data": "media:topdf"},
        ])
        rows.append([
            {"text": "🔤 Read text", "callback_data": "media:ocr"},
            {"text": "🌐 Translate", "callback_data": "media:translate"},
            # Same family as Read text — pull information OUT of the photo. Typing `bill` over an
            # attachment was the wrong shape when this menu already exists.
            {"text": "🧾 Bill", "callback_data": "media:bill"},
            {"text": "⏰ Remind", "callback_data": "media:remind"},
        ])
        # Bottom row: create/learn/share — kept side-by-side (left→right) rather than stacked.
        bottom = [
            {"text": "✨ Effects", "callback_data": "media:effects"},
            {"text": "🎴 Flashcards", "callback_data": "media:fc"},
        ]
        if _social:
            bottom.append({"text": "📣 Post", "callback_data": "media:post"})
        rows.append(bottom)
    if has_pdf:
        rows.append([
            {"text": "🗜 Compress", "callback_data": "media:compress"},
            {"text": "🖼 To images", "callback_data": "media:toimg"},
        ])
        rows.append([
            {"text": "📝 Summarize", "callback_data": "media:summarize"},
            {"text": "🌐 Translate", "callback_data": "media:translate"},
            {"text": "🧾 Bill", "callback_data": "media:bill"},
            {"text": "⏰ Remind", "callback_data": "media:remind"},
        ])
    # Study material (PDF / slide deck / doc) → interactive flashcards quiz. (Images get the
    # Flashcards button grouped into the create/learn row above, so don't duplicate it here.)
    if (has_pdf or has_doc) and not has_image:
        rows.append([{"text": "🎴 Flashcards", "callback_data": "media:fc"}])
    # Video (without an image) gets its own Post row; image's Post is folded into its bottom row.
    if _social and has_video and not has_image:
        rows.append([{"text": "📣 Post to social", "callback_data": "media:post"}])
    return {"inline_keyboard": rows} if rows else None


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
            {"text": "🎵 Extract audio", "callback_data": "media:extractaudio"},
        ],
        [{"text": "🗜✂️ Clip + Compress", "callback_data": "media:clipcompress"}],
    ]}


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
